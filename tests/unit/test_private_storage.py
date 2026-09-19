"""Private historical objects never use public routes or overwrite prior bytes."""
import hashlib
import json

import httpx
import pytest
from tests.fake_storage import PrivateObjects

from market_replay.datasets.chunks import MissingChunk, export_pack_chunks
from market_replay.datasets.pack import PackError
from market_replay.service.datasets import load_stored_pack, publish_pack_chunks
from market_replay.service.storage import StorageError, SupabaseObjects, checked_key


def test_private_routes_hash_verification_and_upload_retry(tmp_path):
    data = b'private history'
    digest = hashlib.sha256(data).hexdigest()
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers['apikey'] == 'server-secret'
        assert request.headers['authorization'] == 'Bearer server-secret'
        if '/bucket/' in request.url.path:
            return httpx.Response(200, json={'public': False})
        if request.method == 'POST':
            assert request.headers['x-upsert'] == 'false'
            return httpx.Response(409)
        assert '/object/authenticated/' in request.url.path
        return httpx.Response(200, content=data)

    objects = SupabaseObjects('https://example.supabase.co', 'server-secret', transport=httpx.MockTransport(handle))
    objects.ensure_private_bucket()
    objects.put_immutable('datasets/a', data, digest)
    target = objects.materialize('datasets/a', digest, tmp_path, 'a')
    assert target.read_bytes() == data
    count = len(requests)
    assert objects.materialize('datasets/a', digest, tmp_path, 'a') == target
    assert len(requests) == count
    with pytest.raises(StorageError, match='hash mismatch'):
        objects.get('datasets/a', '0' * 64)
    with pytest.raises(StorageError, match='mismatched content'):
        objects.put_immutable('datasets/a', b'changed', digest)
    objects.close()


def test_public_bucket_is_refused():
    objects = SupabaseObjects('https://example.supabase.co', 'secret', transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'public': True})))
    with pytest.raises(StorageError, match='private bucket'):
        objects.ensure_private_bucket(create=True)
    objects.close()


@pytest.mark.parametrize('key', ['', '.', '../a', '/a', 'x/../a', 'x//a', 'x\\a'])
def test_unsafe_object_paths_are_refused(key):
    with pytest.raises(StorageError):
        checked_key(key)


def test_verified_metadata_does_not_download_tape(dev_pack_dir, tmp_path):
    output = tmp_path / 'export'
    index = export_pack_chunks(dev_pack_dir, output, max_events=20)
    objects = PrivateObjects()
    descriptor = publish_pack_chunks(output, objects)
    pack = load_stored_pack(descriptor, tmp_path / 'cold', objects)
    assert pack.tape_count == index['total']
    assert not any('/chunks/' in name for name in objects.downloads)
    with pytest.raises(MissingChunk):
        _ = pack.event_source[0]
    assert pack.cl_init_ms == index['cl_init_ms']
    with pytest.raises(PackError, match='budget'):
        publish_pack_chunks(output, objects, max_bytes=1)


def test_metadata_cannot_escape_root(dev_pack_dir, tmp_path):
    output = tmp_path / 'export'
    export_pack_chunks(dev_pack_dir, output)
    objects = PrivateObjects()
    descriptor = publish_pack_chunks(output, objects)
    for bad in ('../escape', '/escape', 'x/y'):
        with pytest.raises((PackError, StorageError)):
            load_stored_pack(descriptor | {'pack_id': bad}, tmp_path / 'cold', objects)
    raw = objects.data[descriptor['prefix'] + '/index.json']
    index = json.loads(raw)
    index['metadata']['../escape'] = next(iter(index['metadata'].values()))
    raw = json.dumps(index).encode()
    objects.data[descriptor['prefix'] + '/index.json'] = raw
    descriptor['index_sha256'] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(StorageError):
        load_stored_pack(descriptor, tmp_path / 'cold', objects)
