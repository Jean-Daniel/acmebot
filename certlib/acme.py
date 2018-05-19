import contextlib
import datetime
import json
import os
import sys
import time
import urllib
from typing import Dict, List, Optional
from urllib import parse

import collections
import josepy
import pkg_resources
from acme import client, messages

from . import AcmeError
from .config import FileManager
from .crypto import PrivateKey
from .logging import log
from .utils import (ArchiveAndWriteOperation, ArchiveOperation, Hooks,
                    WriteOperation, commit_file_transactions, get_key_cipher)


def _user_agent():
    acmelib = pkg_resources.get_distribution('acme')
    return 'certmgr/1.0.0 acme-python/{acme_version}'.format(acme_version=acmelib.version if acmelib else "0.0.0")


class _PasswordProvider(object):

    def __init__(self, passphrase):
        self.key_cipher = None
        self.passphrase = passphrase

    def __call__(self, *args, **kwargs):
        self.key_cipher = get_key_cipher('acme_client', self.passphrase, True)
        return self.key_cipher.passphrase if self.key_cipher else None


def connect_client(resource_dir: str, account: str, directory_url: str, passphrase, archive_dir: Optional[str]) -> client.ClientV2:
    registration = None
    registration_path = os.path.join(resource_dir, 'registration.json')
    with contextlib.suppress(FileNotFoundError), open(registration_path) as f:
        registration = messages.RegistrationResource.json_loads(f.read())
        log.debug('Loaded registration %s', registration_path)
        acme_url = urllib.parse.urlparse(directory_url)
        reg_url = urllib.parse.urlparse(registration.uri)
        if (acme_url[0] != reg_url[0]) or (acme_url[1] != reg_url[1]):
            log.info('ACME service URL has changed, re-registering with new client key')
            registration = None

    ops = []

    client_key = None
    client_key_cipher = None
    client_key_path = os.path.join(resource_dir, 'client.key')
    # ACME-ISSUE: Resetting the client key should not be necessary, but the new registration comes back empty if we use the old key
    if registration:
        pwd = _PasswordProvider(passphrase)
        client_key = PrivateKey.load(client_key_path, pwd)
        client_key_cipher = pwd.key_cipher
        if not client_key:
            # File does not exist, try to load old client file
            try:
                client_old_key_path = os.path.join(resource_dir, 'client_key.json')
                with open(client_old_key_path) as f:
                    # pylint: disable=protected-access
                    client_key = PrivateKey.from_key(josepy.JWKRSA.fields_from_json(json.load(f)).key._wrapped, False)
                log.debug('Loaded old format client key %s', client_old_key_path)
                ops.append(ArchiveOperation('resource', client_old_key_path))
            except FileNotFoundError:
                log.info('Client key not present, generating')
                registration = None
        else:
            log.debug('Loaded client key %s', client_key_path)

    op = None
    if not client_key:
        client_key = PrivateKey.create('rsa', 4096)
        if passphrase and not client_key_cipher:
            client_key_cipher = get_key_cipher('acme_client', passphrase, False)
        op = ArchiveAndWriteOperation('resource', client_key_path, mode=0o600)
    elif client_key.encrypted and not passphrase:
        log.debug("client key is encrypted but config require clear text.")
        op = ArchiveAndWriteOperation('resource', client_key_path, mode=0o600)
        client_key_cipher = None
    elif not client_key.encrypted and passphrase:
        client_key_cipher = get_key_cipher('acme_client', passphrase, False)
        log.debug("client key is clear text but config require encrypted.")
        op = WriteOperation(client_key_path, mode=0o600)

    if op:
        with op.file() as f:
            f.write(client_key.encode(client_key_cipher.passphrase if client_key_cipher else None))
        ops.append(op)

    try:
        net = client.ClientNetwork(josepy.JWKRSA(key=client_key.key), account=registration, user_agent=_user_agent())
        log.debug("Fetching meta from acme server '%s'", directory_url)
        directory = messages.Directory.from_json(net.get(directory_url).json())
        acme_client = client.ClientV2(directory, net)
    except Exception as error:
        raise AcmeError("Can't connect to ACME service") from error

    if not registration:
        log.info('Registering client')
        try:
            reg = messages.NewRegistration.from_data(email=account)
            if "terms_of_service" in acme_client.directory.meta:
                tos = acme_client.directory.meta.terms_of_service
                if sys.stdin.isatty():
                    sys.stdout.write('ACME service has the following terms of service:\n')
                    sys.stdout.write(tos)
                    sys.stdout.write('\n')
                    answer = input('Accept? (Y/n) ')
                    if answer and not answer.lower().startswith('y'):
                        raise Exception('Terms of service rejected.')
                    log.debug('Terms of service accepted.')
                else:
                    log.debug('Terms of service auto-accepted: %s', tos)
                reg = reg.update(terms_of_service_agreed=True)

            registration = acme_client.new_account(reg)
            op = ArchiveAndWriteOperation('resource', registration_path, mode=0o600)
            with op.file(binary=False) as f:
                f.write(registration.json_dumps())
            ops.append(op)
        except Exception as error:
            raise AcmeError("Can't register with ACME service") from error

    if ops:
        commit_file_transactions(ops, archive_dir)

    return acme_client


def _get_challenge(authorization_resource: messages.AuthorizationResource, ty: str) -> Optional[messages.ChallengeBody]:
    for challenge in authorization_resource.body.challenges:
        if ty == challenge.typ:
            return challenge
    return None


def handle_authorizations(order: messages.OrderResource, fs: FileManager, acme_client: client.ClientV2,
                          retry: int, delay: int, hooks: Hooks) -> List[messages.AuthorizationResource]:
    authorizations = []
    authorization_resources = {}

    # collect pending auth resources
    for authorization_resource in order.authorizations:  # type: messages.AuthorizationResource
        domain_name = authorization_resource.body.identifier.value
        if messages.STATUS_VALID == authorization_resource.body.status:
            log.debug('Domain "%s" already authorized', domain_name)
            authorizations.append(authorization_resource)
        elif messages.STATUS_PENDING == authorization_resource.body.status:
            log.info('Requesting authorization for domain "%s"', domain_name)
            authorization_resources[domain_name] = authorization_resource
        else:
            raise AcmeError('Unexpected status "{}" for authorization of {}', authorization_resource.body.status, domain_name)

    # All auth where already valid, nothing to do
    if not authorization_resources:
        return authorizations

    # Setup challenge responses
    challenge_http_responses = {}
    for domain_name, authorization_resource in authorization_resources.items():
        identifier = authorization_resource.body.identifier.value
        http_challenge_directory = fs.http_challenge_directory(identifier)
        if not http_challenge_directory:
            raise AcmeError("no http_challenge_directory directory specified for domain {}", domain_name)
        challenge = _get_challenge(authorization_resource, 'http-01')
        if not challenge:
            raise AcmeError('Unable to use http-01 challenge for {}', domain_name)
        challenge_file_path = os.path.join(http_challenge_directory, challenge.chall.encode('token'))
        log.debug('Setting http acme-challenge for "%s" in file "%s"', domain_name, challenge_file_path)
        try:
            os.makedirs(os.path.dirname(challenge_file_path), 0o755, exist_ok=True)
            with open(challenge_file_path, 'w') as f:
                f.write(challenge.validation(acme_client.net.key))
                os.fchmod(f.fileno(), 0o644)

            challenge_http_responses[domain_name] = challenge_file_path
            hooks.add('set_http_challenge', domain=domain_name, file=challenge_file_path)
        except Exception as error:
            # remove already saved challenges
            for challenge_file in challenge_http_responses.values():
                os.remove(challenge_file)
            raise AcmeError('Unable to create acme-challenge file "{}"', challenge_file_path) from error
    try:
        hooks.call()
        # Process authorizations
        authorizations += _get_authorizations(acme_client, authorization_resources, retry, delay)
    except Exception:
        for challenge_file in challenge_http_responses.values():
            os.remove(challenge_file)
        raise

    # Cleanup challenges
    for domain_name, challenge_file in challenge_http_responses.items():
        log.debug('Removing http acme-challenge for %s', domain_name)
        os.remove(challenge_file)
        hooks.add('clear_http_challenge', domain=domain_name, file=challenge_file)
    hooks.call()

    return authorizations


AuthorizationTuple = collections.namedtuple('AuthorizationTuple', ['datetime', 'domain_name', 'authorization_resource'])


def _get_authorizations(acme_client: client.ClientV2, authorization_resources: Dict[str, messages.AuthorizationResource],
                        retry: int, delay: int) -> List[messages.AuthorizationResource]:
    # answer challenges
    for domain_name, authorization_resource in authorization_resources.items():
        log.debug('Answering challenge for %s', domain_name)
        challenge = _get_challenge(authorization_resource, 'http-01')
        try:
            acme_client.answer_challenge(challenge, challenge.response(acme_client.net.key))
        except Exception as error:
            raise AcmeError('Error answering challenge for {}', domain_name) from error

    # poll for authorizations
    authorizations = []
    waiting = [AuthorizationTuple(datetime.datetime.now(), domain_name, authorization_resource)
               for domain_name, authorization_resource in authorization_resources.items()]
    attempts = collections.defaultdict(int)
    while waiting:
        when, domain_name, authorization_resource = waiting.pop(0)
        now = datetime.datetime.now()
        if now < when:
            seconds = (when - now).seconds
            if 0 < seconds:
                time.sleep(seconds)
                log.debug('Polling for %s', domain_name)
        try:
            authorization_resource, response = acme_client.poll(authorization_resource)
            if 200 != response.status_code:
                log.warning('%s while waiting for domain challenge for %s', response, domain_name)
                waiting.append(AuthorizationTuple(acme_client.retry_after(response, default=delay), domain_name, authorization_resource))
                continue
        except Exception as error:
            raise AcmeError('Error polling for authorization for {}', domain_name) from error

        attempts[authorization_resource] += 1
        if messages.STATUS_VALID == authorization_resource.body.status:
            authorizations.append(authorization_resource)
            log.info('Domain "%s" authorized', domain_name)
            continue
        elif messages.STATUS_INVALID == authorization_resource.body.status:
            error = _get_challenge(authorization_resource, 'http-01').error
            raise AcmeError('Authorization failed for domain {}: {}', domain_name, error.detail if error else 'Unknown error')
        elif messages.STATUS_PENDING == authorization_resource.body.status:
            if attempts[authorization_resource] > retry:
                log.debug('Max retry reached for domain %s', domain_name)
                raise AcmeError('Authorization timed out for {}', domain_name)
            else:
                log.debug('Retrying')
                waiting.append(AuthorizationTuple(acme_client.retry_after(response, default=delay), domain_name, authorization_resource))
        else:
            raise AcmeError('Unexpected authorization status "{}"', authorization_resource.body.status)

    return authorizations
