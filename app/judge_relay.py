"""Three bounded live roles over an owner-hosted HTTPS relay; no provider key.

The existing mission journal records consent and a started attempt before ask.
Neither construction nor binding sends a request. A network error is never retried.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import re
import ssl
import time
from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import judge_setup
import operations_workspace as ops

VERSION = 'agent-rehearsal-judge-relay-v1'
CONNECTION = 'relay_connection_private.json'
MAX_BODY = 131072


def origin(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 250:
        raise ValueError('Use the assigned public HTTPS relay origin.')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', parsed.hostname)
            or '.' not in parsed.hostname or '..' in parsed.hostname
            or parsed.hostname.endswith(('.localhost', '.local', '.internal'))
            or re.fullmatch(r'[0-9.]+', parsed.hostname)):
        raise ValueError('Use a public HTTPS origin without credentials, port or path.')
    return 'https://' + parsed.hostname


def validate(value):
    if not isinstance(value, dict) or set(value) != {'version', 'origin', 'username', 'password'} or value['version'] != VERSION:
        raise ValueError('Use the private judge relay connection settings.')
    if origin(value['origin']) != value['origin']:
        raise ValueError('Use the normalized HTTPS relay origin.')
    if not isinstance(value['username'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value['username']):
        raise ValueError('Use the supplied evaluation username.')
    password = value['password']
    if not isinstance(password, str) or not 24 <= len(password) <= 128 or not re.fullmatch(r'[A-Za-z0-9_-]+', password):
        raise ValueError('Use the supplied URL-safe evaluation password, not a provider API key.')
    return value


def load(directory):
    path = judge_setup.checked_directory(directory) / CONNECTION
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError('The private relay connection is unavailable.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:raise ValueError('Duplicate relay setting.')
            result[key] = value
        return result
    return validate(json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs))


def save(directory, settings):
    directory = judge_setup.checked_directory(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    judge_setup.private_write(directory / CONNECTION, json.dumps(validate(settings), indent=2) + '\n')


class RelayError(ops.factory.GenerationError):
    pass


class RoleClient:
    live = True
    mode = 'LIVE NVIDIA VIA NEBIUS TOKEN FACTORY; OWNER-HOSTED RELAY'

    def __init__(self, role, settings):
        if role not in ops.ROLES:raise ValueError('Unknown registered role.')
        self.role = role
        self._connection = copy.deepcopy(validate(settings))
        self.system_prompt = ops.PROMPTS[role]
        self.request_settings = ops.factory.inference.inference_settings(8192, True)
        self._review = None

    def bind_review(self, review):
        if (review.get('kind') != 'paid_proposal' or review.get('role') != self.role
                or review.get('settings') != self.request_settings
                or review.get('operations_source') != ops.source_digest()
                or review.get('context_digest') != ops.sim.digest(review.get('context'))
                or review.get('digest') != ops.sim.digest({k:v for k,v in review.items() if k != 'digest'})):
            raise ValueError('Bind the exact current proposal review before the relay request.')
        self._review = copy.deepcopy(review)

    def ask(self, context):
        review, self._review = self._review, None
        if review is None or review['context_digest'] != ops.sim.digest(context):
            raise RelayError('RELAY_REVIEW_REQUIRED')
        if review['expires_unix'] <= time.time():raise RelayError('RELAY_REVIEW_EXPIRED')
        workflow = review['mission_id']
        request_id = hashlib.sha256((workflow + ':' + self.role).encode()).hexdigest()
        payload = {'schema_version':VERSION, 'workflow':workflow, 'role':self.role,
                   'request_id':request_id, 'review_digest':review['digest'],
                   'expires_ms':int(review['expires_unix'] * 1000),
                   'prompt_digest':hashlib.sha256(self.system_prompt.encode()).hexdigest(),
                   'context':ops.sim.canonical(context)}
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(body) > MAX_BODY:raise RelayError('RELAY_CONTEXT_TOO_LARGE')
        settings = self._connection
        token = b64encode((settings['username'] + ':' + settings['password']).encode()).decode('ascii')
        connection = http.client.HTTPSConnection(urlsplit(settings['origin']).hostname,
                      timeout=125, context=ssl.create_default_context())
        started = time.monotonic()
        try:
            # HTTPSConnection does not follow redirects, use proxy environment
            # variables, install an SDK or retry an uncertain network request.
            connection.request('POST', '/v1/proposal', body=body,
                headers={'Authorization':'Basic ' + token, 'Content-Type':'application/json', 'Accept':'application/json'})
            response = connection.getresponse()
            if response.status != 200:raise RelayError('RELAY_HTTP_' + str(response.status))
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:raise RelayError('RELAY_RESPONSE_TOO_LARGE')
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get('schema_version') != VERSION
                    or value.get('request_id') != request_id or value.get('role') != self.role
                    or value.get('model_requested') != ops.factory.inference.MODEL
                    or not isinstance(value.get('message'), dict) or not isinstance(value.get('metadata'), dict)):
                raise RelayError('RELAY_RESPONSE_BINDING')
            message = SimpleNamespace(**{k:v for k,v in value['message'].items()
                                       if k in {'content', 'reasoning_content', 'refusal'}})
            content, reasoning, removed = ops.factory.inference.visible_answer(message)
            metadata = {k:value['metadata'].get(k) for k in
                        ('model', 'response_id', 'finish_reason', 'refused', 'input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens')}
            metadata.update(source=self.mode, relay_origin=settings['origin'], relay_request_id=request_id,
                            reasoning_content_present=reasoning, marked_thinking_content_removed=removed,
                            latency_seconds=round(time.monotonic() - started, 3))
            return (ops.factory.inference.redact(content, settings['password']),
                    ops.factory.inference.redact(metadata, settings['password']))
        except RelayError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            raise RelayError('RELAY_REQUEST_UNCONFIRMED_NO_RETRY') from None
        finally:
            connection.close()


def providers(settings):
    return {role:RoleClient(role, settings) for role in ops.ROLES}
