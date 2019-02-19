from datetime import datetime as dt, timedelta

import pytest

from splitgraph.core import clone, select, ResultShape, SPLITGRAPH_META_SCHEMA
from splitgraph.exceptions import SplitGraphException
from test.splitgraph.commands.test_layered_querying import prepare_lq_repo


def _get_refcount(object_manager, object_id):
    return object_manager.object_engine.run_sql(select("object_cache_status", "refcount", "object_id = %s"),
                                                (object_id,), return_shape=ResultShape.ONE_ONE)


def _get_last_used(object_manager, object_id):
    return object_manager.object_engine.run_sql(select("object_cache_status", "last_used", "object_id = %s"),
                                                (object_id,), return_shape=ResultShape.ONE_ONE)


def _setup_object_cache_test(pg_repo_remote, longer_chain=False):
    pg_repo_local = clone(pg_repo_remote)
    pg_repo_local.images['latest'].checkout()
    prepare_lq_repo(pg_repo_local, include_snap=False, commit_after_every=False, include_pk=True)
    if longer_chain:
        pg_repo_local.run_sql("INSERT INTO FRUITS VALUES (4, 'kumquat')")
        pg_repo_local.commit()

    # Same setup as the LQ test in the beginning: we clone a repo from upstream, don't download anything, all
    # objects are on Minio.
    remote = pg_repo_local.push(handler='S3', handler_options={})
    pg_repo_local.rm()
    pg_repo_remote.objects.delete_objects(remote.objects.get_downloaded_objects())
    pg_repo_remote.engine.commit()
    pg_repo_local.objects.cleanup()
    pg_repo_local = clone(pg_repo_remote, download_all=False)

    # 6 objects in the tree (SNAP -> SNAP -> DIFF for both tables)
    assert len(pg_repo_local.objects.get_existing_objects()) == 6 if not longer_chain else 7
    assert len(pg_repo_local.objects.get_downloaded_objects()) == 0
    assert len(remote.objects.get_existing_objects()) == 6 if not longer_chain else 7
    assert len(remote.objects.get_downloaded_objects()) == 0

    # Nothing has yet been downloaded (cache entries only for externally downloaded things)
    assert len(pg_repo_local.engine.run_sql("SELECT * FROM splitgraph_meta.object_cache_status")) == 0

    return pg_repo_local


def test_object_cache_loading(local_engine_empty, pg_repo_remote):
    # Test object caching, downloading etc.
    pg_repo_local = _setup_object_cache_test(pg_repo_remote)

    object_manager = pg_repo_local.objects
    object_tree = object_manager.get_full_object_tree()
    fruits_v3 = pg_repo_local.images['latest'].get_table('fruits')
    fruits_v2 = pg_repo_local.images[pg_repo_local.images['latest'].parent_id].get_table('fruits')

    # Quick assertions on the objects and table sizes recorded by the engine, since we'll rely on them
    # to test eviction.
    assert len(fruits_v3.objects) == 1
    fruit_diff = fruits_v3.get_object('DIFF')
    assert len(fruits_v2.objects) == 1
    fruit_snap = fruits_v2.get_object('SNAP')
    # Reported by Postgres itself and stored by the engine in object_tree. Might really backfire on us on different
    # Postgres versions.
    assert object_tree[fruit_diff] == ([fruit_snap], 'DIFF', 8192)
    assert object_tree[fruit_snap] == ([], 'SNAP', 8192)

    # Resolve and download the old version: only one SNAP should be downloaded.
    with object_manager.ensure_objects(fruits_v2) as (snap, diffs):
        assert snap == fruit_snap
        assert diffs == []

        assert _get_refcount(object_manager, fruit_snap) == 1
        # fruit_diff not in the cache at all
        assert _get_refcount(object_manager, fruit_diff) is None

    # Exit from the manager: refcounts are 0 but the object is still in the cache since there's enough space.
    assert _get_refcount(object_manager, fruit_snap) == 0
    assert _get_refcount(object_manager, fruit_diff) is None
    assert len(object_manager.get_downloaded_objects()) == 1

    # Resolve and download the new version: will download the DIFF.
    with object_manager.ensure_objects(fruits_v3) as (snap, diffs):
        assert snap == fruit_snap
        assert diffs == [fruit_diff]

        assert _get_refcount(object_manager, fruit_snap) == 1
        assert _get_refcount(object_manager, fruit_diff) == 1
        assert len(object_manager.get_downloaded_objects()) == 2

    assert _get_refcount(object_manager, fruit_snap) == 0
    assert _get_refcount(object_manager, fruit_diff) == 0
    assert len(object_manager.get_downloaded_objects()) == 2


def test_object_cache_eviction(local_engine_empty, pg_repo_remote):
    pg_repo_local = _setup_object_cache_test(pg_repo_remote)

    object_manager = pg_repo_local.objects
    fruits_v3 = pg_repo_local.images['latest'].get_table('fruits')
    fruits_v2 = pg_repo_local.images[pg_repo_local.images['latest'].parent_id].get_table('fruits')
    vegetables_v2 = pg_repo_local.images[pg_repo_local.images['latest'].parent_id].get_table('vegetables')
    vegetables_snap = vegetables_v2.get_object('SNAP')
    fruit_diff = fruits_v3.get_object('DIFF')
    fruit_snap = fruits_v2.get_object('SNAP')

    # Check another test object has the same size
    assert object_manager.get_full_object_tree()[vegetables_snap][2] == 8192

    # Load the fruits objects into the cache
    object_manager.ensure_objects(fruits_v3)

    # Pretend that the cache has no space and try getting a different table
    # Free space is now 0, so we need to run eviction.
    object_manager.cache_size = 8192 * 2
    with object_manager.ensure_objects(vegetables_v2) as (snap, diffs):
        current_objects = object_manager.get_downloaded_objects()
        assert len(current_objects) == 1
        assert fruit_snap not in current_objects
        assert fruit_diff not in current_objects
        assert vegetables_snap in current_objects
        assert vegetables_snap == snap
        assert diffs == []
        assert object_manager.object_engine.table_exists(SPLITGRAPH_META_SCHEMA, vegetables_snap)

    assert _get_refcount(object_manager, vegetables_snap) == 0

    # Now, let's try squeezing the cache even more so that there's only space for one object.
    object_manager.cache_size = 8192
    with object_manager.ensure_objects(fruits_v2):
        # We only need to load the original SNAP here, so we're fine.
        assert object_manager.object_engine.table_exists(SPLITGRAPH_META_SCHEMA, fruit_snap)
        assert not object_manager.object_engine.table_exists(SPLITGRAPH_META_SCHEMA, vegetables_snap)
        assert not object_manager.object_engine.table_exists(SPLITGRAPH_META_SCHEMA, fruit_diff)
        assert len(object_manager.get_downloaded_objects()) == 1

    # Delete all objects
    object_manager.run_eviction(object_manager.get_full_object_tree(), [], None)
    assert len(object_manager.get_downloaded_objects()) == 0

    # Loading the next version (DIFF + SNAP) (not enough space for 2 objects).
    with pytest.raises(SplitGraphException) as ex:
        with object_manager.ensure_objects(fruits_v3):
            pass
    assert "Not enough space in the cache" in str(ex)


def test_object_cache_nested(local_engine_empty, pg_repo_remote):
    # Test that we can have multiple groups of objects loaded at the same time.
    pg_repo_local = _setup_object_cache_test(pg_repo_remote)

    object_manager = pg_repo_local.objects
    fruits_v3 = pg_repo_local.images['latest'].get_table('fruits')
    fruits_v2 = pg_repo_local.images[pg_repo_local.images['latest'].parent_id].get_table('fruits')
    vegetables_v2 = pg_repo_local.images[pg_repo_local.images['latest'].parent_id].get_table('vegetables')
    vegetables_snap = vegetables_v2.get_object('SNAP')
    fruit_diff = fruits_v3.get_object('DIFF')
    fruit_snap = fruits_v2.get_object('SNAP')

    with object_manager.ensure_objects(fruits_v3):
        with object_manager.ensure_objects(vegetables_v2):
            assert _get_refcount(object_manager, fruit_diff) == 1
            assert _get_refcount(object_manager, fruit_snap) == 1
            assert _get_refcount(object_manager, vegetables_snap) == 1
            assert len(object_manager.get_downloaded_objects()) == 3

    # Now evict everything from the cache.
    object_manager.run_eviction(object_manager.get_full_object_tree(), keep_objects=[], required_space=None)
    assert len(object_manager.get_downloaded_objects()) == 0

    object_manager.cache_size = 8192 * 2
    with object_manager.ensure_objects(fruits_v3):
        # Now the fruits objects are being used and so we can't reclaim that space and have to raise an error.
        with pytest.raises(SplitGraphException) as ex:
            with object_manager.ensure_objects(vegetables_v2):
                pass
        assert "Not enough space will be reclaimed" in str(ex)


def test_object_cache_eviction_priority(local_engine_empty, pg_repo_remote):
    pg_repo_local = _setup_object_cache_test(pg_repo_remote)

    object_manager = pg_repo_local.objects
    fruits_v2 = pg_repo_local.images[pg_repo_local.images['latest'].parent_id].get_table('fruits')
    fruits_v3 = pg_repo_local.images['latest'].get_table('fruits')
    fruit_snap = fruits_v2.get_object('SNAP')
    fruit_diff = fruits_v3.get_object('DIFF')
    vegetables_v2 = pg_repo_local.images[pg_repo_local.images['latest'].parent_id].get_table('vegetables')
    vegetables_v3 = pg_repo_local.images['latest'].get_table('vegetables')
    vegetables_snap = vegetables_v2.get_object('SNAP')
    vegetables_diff = vegetables_v3.get_object('DIFF')

    # Setup: the cache has enough space for 3 objects
    object_manager.cache_size = 8192 * 3

    # Can't use time-freezing methods (e.g. freezegun) here since we interact with the Minio server which timestamps
    # everything with the actual current time and kindly tells us to go away when we show up from the past and ask
    # to upload or download objects.

    with object_manager.ensure_objects(fruits_v3):
        lu_1 = _get_last_used(object_manager, fruit_snap)
        assert lu_1 == _get_last_used(object_manager, fruit_diff)

        current_objects = object_manager.get_downloaded_objects()
        assert fruit_snap in current_objects
        assert fruit_diff in current_objects
        assert vegetables_snap not in current_objects
        assert vegetables_diff not in current_objects

    # Slightly later: fetch just the old version (the SNAP)
    with object_manager.ensure_objects(fruits_v2):
        # Make sure the timestamp for the SNAP has been bumped.
        lu_2 = _get_last_used(object_manager, fruit_snap)
        assert _get_last_used(object_manager, fruit_diff) == lu_1
        assert lu_2 > lu_1

        # None of the fruits have been evicted yet.
        current_objects = object_manager.get_downloaded_objects()
        assert fruit_snap in current_objects
        assert fruit_diff in current_objects
        assert vegetables_snap not in current_objects
        assert vegetables_diff not in current_objects

    # Now, fetch the new vegetables version (2 objects). Since we already have
    # 2 objects in the cache and the limit is 3, one must be evicted.
    with object_manager.ensure_objects(vegetables_v3):
        assert _get_last_used(object_manager, fruit_snap) == lu_2
        assert _get_last_used(object_manager, fruit_diff) is None
        lu_3 = _get_last_used(object_manager, vegetables_snap)
        assert lu_3 > lu_2
        assert _get_last_used(object_manager, vegetables_diff) == lu_3

        # The fruit SNAP was used more recently than the DIFF and they both have the same size,
        # so the SNAP will stay.
        current_objects = object_manager.get_downloaded_objects()
        assert fruit_snap in current_objects
        assert fruit_diff not in current_objects
        assert vegetables_snap in current_objects
        assert vegetables_diff in current_objects

        with pytest.raises(SplitGraphException) as ex:
            # Try to load all 4 objects in the same time: should fail.
            with object_manager.ensure_objects(fruits_v3):
                pass
        assert "Not enough space will be reclaimed" in str(ex)


def test_object_cache_snaps(local_engine_empty, pg_repo_remote):
    # Test that asking the cache multiple times for a DIFF object eventually gets it to return SNAPs.
    pg_repo_local = _setup_object_cache_test(pg_repo_remote)

    object_manager = pg_repo_local.objects
    fruits_v2 = pg_repo_local.images[pg_repo_local.images['latest'].parent_id].get_table('fruits')
    fruits_v3 = pg_repo_local.images['latest'].get_table('fruits')
    fruit_snap = fruits_v2.get_object('SNAP')
    fruit_diff = fruits_v3.get_object('DIFF')

    assert object_manager._get_snap_cache() == {}

    # First, ask the cache 4 times for a resolution (expect DIFF chain).
    for i in range(4):
        with object_manager.ensure_objects(fruits_v3) as (snap, diffs):
            assert object_manager._recent_snap_cache_misses(
                fruit_diff, dt.utcnow() - timedelta(seconds=object_manager.cache_misses_lookback)) == i + 1
            assert snap == fruit_snap
            assert diffs == [fruit_diff]

    # This time, the cache should give us a brand new SNAP.
    with object_manager.ensure_objects(fruits_v3) as (tmp_snap, diffs):
        assert tmp_snap != fruit_snap
        assert diffs == []
        assert tmp_snap in object_manager.get_downloaded_objects()
        # The cache now contains the new SNAP, mapped to the fruits_v3's DIFF chain; the snap has the size 8192.
        assert object_manager._get_snap_cache() == {tmp_snap: (fruit_diff, 8192)}

        # Check we're only counting the new SNAP and none of the old objects.
        assert _get_refcount(object_manager, tmp_snap) == 1
        assert _get_refcount(object_manager, fruit_diff) == 0
        assert _get_refcount(object_manager, fruit_snap) == 0

    # Check that if we ask for a chain again, the same cached SNAP is returned.
    with object_manager.ensure_objects(fruits_v3) as (snap, diffs):
        assert snap == tmp_snap
        assert diffs == []
        assert len(object_manager.get_downloaded_objects()) == 3

        # Make sure the cache hasn't changed.
        assert object_manager._get_snap_cache() == {tmp_snap: (fruit_diff, 8192)}


def test_object_cache_snaps_eviction(local_engine_empty, pg_repo_remote):
    # Test the temporarily materialized SNAPs get evicted when they're not needed.
    pg_repo_local = _setup_object_cache_test(pg_repo_remote)

    object_manager = pg_repo_local.objects
    fruits_v2 = pg_repo_local.images[pg_repo_local.images['latest'].parent_id].get_table('fruits')
    fruits_v3 = pg_repo_local.images['latest'].get_table('fruits')
    fruit_snap = fruits_v2.get_object('SNAP')
    fruit_diff = fruits_v3.get_object('DIFF')
    vegetables_v2 = pg_repo_local.images[pg_repo_local.images['latest'].parent_id].get_table('vegetables')
    vegetables_v3 = pg_repo_local.images['latest'].get_table('vegetables')
    vegetables_snap = vegetables_v2.get_object('SNAP')
    vegetables_diff = vegetables_v3.get_object('DIFF')

    assert object_manager._get_snap_cache() == {}

    # Poke the cache to get it to generate a SNAP
    for i in range(5):
        with object_manager.ensure_objects(fruits_v3):
            pass
    assert len(object_manager.get_downloaded_objects()) == 3

    # Only space for 2 objects (we currently have 3), so a future cache interaction will trigger an eviction.
    object_manager.cache_size = 8192 * 2

    # Check that one of the old objects gets evicted and the temporary SNAP remains.
    with object_manager.ensure_objects(fruits_v3) as (snap, diffs):
        assert len(object_manager.get_downloaded_objects()) == 2
        assert snap != fruit_snap

    # Now, load vegetables_v2 (only results in 1 object, the original SNAP, being downloaded).
    with object_manager.ensure_objects(vegetables_v2) as (snap, diffs):
        # Since the cached SNAP was slightly more recently used, the original fruit SNAP and DIFF will be evicted
        # instead.
        assert snap == vegetables_snap
        downloaded_objects = object_manager.get_downloaded_objects()
        assert fruit_snap not in downloaded_objects
        assert fruit_diff not in downloaded_objects
        assert vegetables_snap in downloaded_objects
        assert len(downloaded_objects) == 2

    # Now, load vegetables_v3: since both objects now need to be in the cache,
    # the temporary snap has to be evicted.
    with object_manager.ensure_objects(vegetables_v3):
        downloaded_objects = object_manager.get_downloaded_objects()
        assert vegetables_snap in downloaded_objects
        assert vegetables_diff in downloaded_objects
        assert len(downloaded_objects) == 2

        assert object_manager._get_snap_cache() == {}


def test_object_cache_snaps_cleanup_keeps(local_engine_empty, pg_repo_remote):
    pg_repo_local = _setup_object_cache_test(pg_repo_remote)
    object_manager = pg_repo_local.objects
    fruits_v3 = pg_repo_local.images['latest'].get_table('fruits')
    # Poke the cache to get it to generate a SNAP
    for i in range(5):
        with object_manager.ensure_objects(fruits_v3):
            pass
    assert len(object_manager.get_downloaded_objects()) == 3

    # Run cleanup() and make sure that (since the cached SNAP is still linked to a DIFF that's still required)
    # nothing gets deleted.
    object_manager.cleanup()

    assert len(object_manager._get_snap_cache().items()) == 1
    assert len(object_manager.get_downloaded_objects()) == 3


def test_object_cache_snaps_cleanup_cleans(local_engine_empty, pg_repo_remote):
    pg_repo_local = _setup_object_cache_test(pg_repo_remote)
    object_manager = pg_repo_local.objects
    fruits_v3 = pg_repo_local.images['latest'].get_table('fruits')
    # Poke the cache to get it to generate a SNAP
    for i in range(5):
        with object_manager.ensure_objects(fruits_v3):
            pass
    pg_repo_local.rm()
    # This time, since the DIFF that the cached SNAP is linked to doesn't exist, the cache entry
    # and the actual SNAP should get deleted too.

    object_manager.cleanup()
    assert len(object_manager._get_snap_cache().items()) == 0
    assert len(object_manager.get_downloaded_objects()) == 0


def test_object_cache_snaps_longer_chain(local_engine_empty, pg_repo_remote):
    # Test a longer DIFF chain
    pg_repo_local = _setup_object_cache_test(pg_repo_remote, longer_chain=True)

    object_manager = pg_repo_local.objects
    log = pg_repo_local.images['latest'].get_log()
    fruits_v2 = log[2].get_table('fruits')
    fruits_v3 = log[1].get_table('fruits')
    fruits_v4 = log[0].get_table('fruits')
    fruit_snap = fruits_v2.get_object('SNAP')
    fruit_diff = fruits_v3.get_object('DIFF')
    fruit_diff_2 = fruits_v4.get_object('DIFF')

    assert object_manager._get_snap_cache() == {}

    # First, test hitting the previous version (SNAP -> DIFF)
    for i in range(4):
        with object_manager.ensure_objects(fruits_v3) as (snap, diffs):
            assert snap == fruit_snap
            assert diffs == [fruit_diff]
    with object_manager.ensure_objects(fruits_v3) as (tmp_snap, diffs):
        assert tmp_snap != fruit_snap
        assert diffs == []
        assert _get_refcount(object_manager, tmp_snap) == 1
        assert _get_refcount(object_manager, fruit_diff) == 0
        assert _get_refcount(object_manager, fruit_snap) == 0
        assert object_manager._get_snap_cache() == {tmp_snap: (fruit_diff, 8192)}

    # Check the temporary snap is used in the resolution (even when it's not being created).
    with object_manager.ensure_objects(fruits_v3) as (snap, diffs):
        assert snap == tmp_snap
        assert diffs == []

    # Now test requesting the next version -- for the first 4 requests it should still return a DIFF
    # chain, but based on the cached SNAP (since then we only have 1 DIFF to apply instead of 2)
    for i in range(4):
        with object_manager.ensure_objects(fruits_v4) as (snap, diffs):
            assert snap == tmp_snap
            assert diffs == [fruit_diff_2]

    # At this point the SNAP should be cached.
    with object_manager.ensure_objects(fruits_v4) as (tmp_snap_2, diffs):
        assert tmp_snap_2 != fruit_snap
        assert tmp_snap_2 != fruit_diff
        assert diffs == []
        assert _get_refcount(object_manager, tmp_snap_2) == 1
        assert _get_refcount(object_manager, tmp_snap) == 0
        assert _get_refcount(object_manager, fruit_diff) == 0
        assert _get_refcount(object_manager, fruit_diff_2) == 0
        assert _get_refcount(object_manager, fruit_snap) == 0
        assert object_manager._get_snap_cache() == {
            tmp_snap: (fruit_diff, 8192),
            tmp_snap_2: (fruit_diff_2, 8192)
        }
        assert len(object_manager.get_downloaded_objects()) == 5  # Original SNAP, 2 DIFFs and 2 derived SNAPs.

    # Test again this all still works after the cache has been populated
    with object_manager.ensure_objects(fruits_v3) as (snap, diffs):
        assert snap == tmp_snap
        assert diffs == []
    with object_manager.ensure_objects(fruits_v4) as (snap, diffs):
        assert snap == tmp_snap_2
        assert diffs == []

    # Test eviction as well -- make sure to get the original version of vegetables with just the SNAP
    # (the third image from the top).
    # Cache has space for 3 objects and since the 2 derived SNAPs were most recently used, they get to stay.
    object_manager.cache_size = 8192 * 3
    vegetables_v2 = pg_repo_local.images['latest'].get_log()[2].get_table('vegetables')
    with object_manager.ensure_objects(vegetables_v2) as (snap, diffs):
        assert diffs == []
        downloaded_objects = object_manager.get_downloaded_objects()
        assert len(downloaded_objects) == 3
        assert tmp_snap in downloaded_objects
        assert tmp_snap_2 in downloaded_objects
        assert snap in downloaded_objects