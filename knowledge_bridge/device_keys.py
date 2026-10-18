"""Additive DPAPI data-key custody; existing password/recovery slots stay intact."""
import base64
import hashlib
import json
from pathlib import Path
from .windows_trust import dpapi, DeviceTrustUnavailable
from .protected_storage import _atomic_json


class DeviceKeyStore:
    def __init__(self, directory: Path, workspace_id: str):
        self.path = directory / (workspace_id + '.json')
        self.workspace_id = workspace_id
        self.entropy = ('jiedu-device-key-v1:' + workspace_id).encode()

    @staticmethod
    def binding(keyring):
        return hashlib.sha256(json.dumps(keyring['slots']['recovery'], sort_keys=True).encode()).hexdigest()

    def read(self, keyring):
        value = json.loads(self.path.read_text('utf-8'))
        if value.get('format') != 1 or value.get('workspace_id') != self.workspace_id:
            raise DeviceTrustUnavailable('设备托管材料不属于此工作区')
        raw = dpapi(base64.b64decode(value['sealed'], validate=True), self.entropy, decrypt=True)
        payload = json.loads(raw)
        if payload.get('binding') != self.binding(keyring):
            raise DeviceTrustUnavailable('设备托管与现有恢复密钥不匹配')
        key = base64.b64decode(payload['key'], validate=True)
        if len(key) != 32:
            raise DeviceTrustUnavailable('设备托管材料无效')
        return key

    def save(self, key, keyring):
        payload = json.dumps({'key': base64.b64encode(key).decode(), 'binding': self.binding(keyring)}).encode()
        sealed = dpapi(payload, self.entropy)
        if dpapi(sealed, self.entropy, decrypt=True) != payload:
            raise DeviceTrustUnavailable('设备托管回读验证失败，原密钥未改变')
        value = {'format': 1, 'workspace_id': self.workspace_id,
                 'protection': 'Windows DPAPI CurrentUser', 'sealed': base64.b64encode(sealed).decode()}
        if self.path.exists():
            # Preserve only encrypted custody material; never plaintext key or password.
            backup = self.path.with_suffix('.previous.json')
            _atomic_json(backup, json.loads(self.path.read_text('utf-8')))
        _atomic_json(self.path, value)

