import io
import json
import os
import pathlib
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
import sys
sys.path.insert(0, str(ROOT / "src" / "gateway"))

import state_sync


class StateSyncArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name) / "home"
        self.home.mkdir()
        self.config = state_sync.write_config(self.home, state_sync.DEFAULT_CONFIG)

    def test_default_whitelist_preserves_memory_skills_and_future_files(self):
        (self.home / "memories").mkdir()
        (self.home / "memories" / "MEMORY.md").write_text("remembered")
        (self.home / "skills" / "new-skill" / "references").mkdir(parents=True)
        (self.home / "skills" / "new-skill" / "SKILL.md").write_text("skill")
        (self.home / "skills" / "new-skill" / "references" / "future.md").write_text("future")
        (self.home / "SOUL.md").write_text("identity")

        archive, _digest, count = state_sync.build_archive(self.home, self.config)

        self.assertEqual(6, count)  # legacy config, sparse overrides, and four durable files
        target = pathlib.Path(self.temp.name) / "restored"
        restored = state_sync.restore_archive(target, archive, self.config)
        self.assertEqual(6, restored)
        self.assertEqual(
            "future",
            (target / "skills" / "new-skill" / "references" / "future.md").read_text(),
        )

    def test_hard_exclusions_override_an_edited_whitelist(self):
        config = state_sync.validate_config({
            **state_sync.DEFAULT_CONFIG,
            "include_paths": ["memories", "skills", "sessions", ".env", "state.db"],
        })
        (self.home / "memories").mkdir()
        (self.home / "memories" / "MEMORY.md").write_text("safe")
        (self.home / "sessions").mkdir()
        (self.home / "sessions" / "private.json").write_text("private conversation")
        (self.home / ".env").write_text("SECRET=value")
        (self.home / "state.db").write_bytes(b"private database")

        selected = {name for name, _ in state_sync.selected_files(self.home, config)}

        self.assertIn("memories/MEMORY.md", selected)
        self.assertNotIn("sessions/private.json", selected)
        self.assertNotIn(".env", selected)
        self.assertNotIn("state.db", selected)

    def test_curator_backups_and_symlinks_are_not_archived(self):
        skill = self.home / "skills" / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("skill")
        backup = self.home / "skills" / ".curator_backups" / "old"
        backup.mkdir(parents=True)
        (backup / "skills.tar.gz").write_bytes(b"duplicate")
        outside = pathlib.Path(self.temp.name) / "outside.txt"
        outside.write_text("outside")
        os.symlink(outside, skill / "escape.txt")

        selected = {name for name, _ in state_sync.selected_files(self.home, self.config)}

        self.assertIn("skills/example/SKILL.md", selected)
        self.assertNotIn("skills/.curator_backups/old/skills.tar.gz", selected)
        self.assertNotIn("skills/example/escape.txt", selected)

    def test_restore_rejects_path_traversal(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as archive:
            info = tarfile.TarInfo("../escape")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"nope"))

        with self.assertRaises(state_sync.StateSyncError):
            state_sync.restore_archive(self.home, raw.getvalue(), self.config)
        self.assertFalse((pathlib.Path(self.temp.name) / "escape").exists())

    def test_restore_rejects_content_that_does_not_match_manifest(self):
        manifest = json.dumps({
            "format": 1,
            "files": {
                "memories/MEMORY.md": {
                    "sha256": "0" * 64,
                    "size": 7,
                },
            },
        }).encode()
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as archive:
            info = tarfile.TarInfo(state_sync.ARCHIVE_MANIFEST)
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))
            info = tarfile.TarInfo("memories/MEMORY.md")
            info.size = 7
            archive.addfile(info, io.BytesIO(b"changed"))

        with self.assertRaises(state_sync.StateSyncError):
            state_sync.restore_archive(self.home, raw.getvalue(), self.config)

    def test_archive_is_byte_stable_after_restore(self):
        (self.home / "memories").mkdir()
        (self.home / "memories" / "MEMORY.md").write_text("stable")
        first, first_hash, _ = state_sync.build_archive(self.home, self.config)
        restored = pathlib.Path(self.temp.name) / "stable-restore"
        state_sync.restore_archive(restored, first, self.config)

        second, second_hash, _ = state_sync.build_archive(
            restored, state_sync.load_config(restored))

        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first, second)

    def test_archive_name_contains_no_tenant_user_or_agent_identifier(self):
        self.assertEqual("hermes-owner-state-v1.tar.gz", state_sync.ARCHIVE_NAME)

    def test_config_update_is_atomic_and_bounded(self):
        updated = state_sync.write_config(self.home, {
            **self.config,
            "include_paths": [*self.config["include_paths"], "future-durable-state"],
            "max_archive_bytes": 5_000_000,
        })

        self.assertIn("future-durable-state", updated["include_paths"])
        self.assertEqual(updated, json.loads((self.home / "state-sync.json").read_text()))
        with self.assertRaises(ValueError):
            state_sync.validate_config({
                **updated,
                "include_paths": ["../outside"],
            })

    def test_sparse_overrides_inherit_new_packaged_defaults(self):
        state_sync.write_config(self.home, {
            **self.config,
            "include_paths": [*self.config["include_paths"], "owner-added"],
        })
        packaged = pathlib.Path(self.temp.name) / "new-defaults.json"
        packaged.write_text(json.dumps({
            **state_sync.DEFAULT_CONFIG,
            "include_paths": [*state_sync.DEFAULT_CONFIG["include_paths"], "new-runtime-state"],
        }))

        with mock.patch.dict(os.environ, {"STATE_SYNC_DEFAULTS_PATH": str(packaged)}):
            effective = state_sync.load_config(self.home)

        self.assertIn("owner-added", effective["include_paths"])
        self.assertIn("new-runtime-state", effective["include_paths"])

    def test_legacy_complete_config_migrates_without_changing_behavior(self):
        (self.home / state_sync.OVERRIDES_FILENAME).unlink()
        legacy = state_sync.validate_config({
            **self.config,
            "include_paths": ["memories", "skills", "owner-only-state"],
            "post_turn_settle_seconds": 0,
        })
        (self.home / state_sync.CONFIG_FILENAME).write_text(json.dumps(legacy))

        migrated = state_sync.ensure_config_migrated(self.home)

        self.assertEqual(legacy, migrated)
        self.assertTrue((self.home / state_sync.OVERRIDES_FILENAME).exists())
        self.assertEqual(legacy, state_sync.load_config(self.home))

    def test_new_safe_top_level_state_is_discoverable_but_secrets_are_not(self):
        (self.home / "future-useful-state").mkdir()
        (self.home / "future-useful-state" / "data.json").write_text("{}")
        (self.home / ".env").write_text("SECRET=value")
        (self.home / "logs").mkdir()

        candidates = state_sync.unlisted_durable_candidates(self.home, self.config)

        self.assertIn("future-useful-state", candidates)
        self.assertNotIn(".env", candidates)
        self.assertNotIn("logs", candidates)

    def test_concurrent_changes_preserve_both_versions_and_retry_with_latest_etag(self):
        config = state_sync.write_config(self.home, {
            **self.config,
            "post_turn_settle_seconds": 0,
        })
        (self.home / "memories").mkdir()
        memory = self.home / "memories" / "MEMORY.md"
        memory.write_text("base")
        base, base_digest, _ = state_sync.build_archive(self.home, config)

        remote_home = pathlib.Path(self.temp.name) / "remote"
        remote_home.mkdir()
        state_sync.write_config(remote_home, config)
        (remote_home / "memories").mkdir()
        (remote_home / "memories" / "MEMORY.md").write_text("remote")
        remote, _remote_digest, _ = state_sync.build_archive(remote_home, config)

        memory.write_text("local")
        messages = []
        manager = state_sync.StateSyncManager(self.home, logger=messages.append)
        manager._prepared = True
        manager._write_blocked = False
        manager._legacy_etag = '"remote-v1"'
        manager._uploaded_hash = base_digest
        manager._base_hashes = state_sync._read_archive(base, config).hashes

        with mock.patch.object(manager, "_upload", side_effect=[
            state_sync.StateSyncConflict("changed"), '"remote-v3"',
        ]) as upload, mock.patch.object(
            manager, "_metadata", return_value=state_sync.RemoteArchive(
                etag='"remote-v2"', size=len(remote),
                download_url="https://tenant.sharepoint.com/state",
            ),
        ), mock.patch.object(manager, "_download", return_value=remote), mock.patch.object(
            manager, "_publish_pointer", return_value=True,
        ):
            manager.finish_turn("short-lived-token")

        self.assertEqual(2, upload.call_count)
        self.assertEqual('"remote-v2"', upload.call_args_list[1].args[2])
        self.assertEqual('"remote-v3"', manager._legacy_etag)
        self.assertEqual("local", memory.read_text())
        preserved = list((self.home / "pending" / "memory" / "state-sync-conflicts").rglob(
            "MEMORY.md"))
        self.assertEqual(1, len(preserved))
        self.assertEqual("remote", preserved[0].read_text())
        self.assertTrue(any("merged concurrent owner state" in message for message in messages))

    def test_release_80_write_after_pointer_is_selected_as_new_head(self):
        pointer_home = pathlib.Path(self.temp.name) / "pointer"
        pointer_home.mkdir()
        pointer_config = state_sync.write_config(pointer_home, self.config)
        (pointer_home / "memories").mkdir()
        (pointer_home / "memories" / "MEMORY.md").write_text("pointer")
        pointer_content, pointer_digest, _ = state_sync.build_archive(
            pointer_home, pointer_config)

        legacy_home = pathlib.Path(self.temp.name) / "legacy"
        legacy_home.mkdir()
        legacy_config = state_sync.write_config(legacy_home, self.config)
        (legacy_home / "memories").mkdir()
        (legacy_home / "memories" / "MEMORY.md").write_text("new legacy write")
        legacy_content, _legacy_digest, _ = state_sync.build_archive(legacy_home, legacy_config)

        pointer = {
            "format": 2,
            "state_schema": 1,
            "component_schemas": dict(state_sync.COMPONENT_SCHEMAS),
            "snapshot": {
                "name": f"{state_sync.SNAPSHOT_PREFIX}{pointer_digest}.tar.gz",
                "sha256": pointer_digest,
                "size": len(pointer_content),
            },
            "history": [],
            "legacy_etag": '"legacy-before"',
            "writer": {"release": "old", "protocol": "dual-v1-v2"},
        }
        manager = state_sync.StateSyncManager(self.home)
        pointer_remote = state_sync.RemoteArchive(
            '"pointer-etag"', 10, "https://tenant.sharepoint.com/p")
        with mock.patch.object(manager, "_metadata_name", return_value=pointer_remote), \
                mock.patch.object(manager, "_read_pointer_document", return_value=pointer), \
                mock.patch.object(manager, "_metadata", return_value=state_sync.RemoteArchive(
            '"legacy-after"', len(legacy_content), "https://tenant.sharepoint.com/l",
        )), mock.patch.object(manager, "_download", return_value=legacy_content):
            head = manager._remote_head("token", self.config)

        self.assertIsNotNone(head)
        self.assertEqual(legacy_content, head.content)
        self.assertTrue(head.needs_pointer)

    def test_prepare_refreshes_remote_state_before_each_owner_turn(self):
        base_home = pathlib.Path(self.temp.name) / "base"
        base_home.mkdir()
        base_config = state_sync.write_config(base_home, self.config)
        (base_home / "memories").mkdir()
        (base_home / "memories" / "MEMORY.md").write_text("base")
        base_content, base_digest, _ = state_sync.build_archive(base_home, base_config)

        remote_home = pathlib.Path(self.temp.name) / "remote-refresh"
        remote_home.mkdir()
        remote_config = state_sync.write_config(remote_home, self.config)
        (remote_home / "memories").mkdir()
        (remote_home / "memories" / "MEMORY.md").write_text("remote update")
        remote_content, remote_digest, _ = state_sync.build_archive(remote_home, remote_config)

        first = state_sync.RemoteStateHead(
            base_content, base_digest, '"l1"', '"p1"', None, False)
        second = state_sync.RemoteStateHead(
            remote_content, remote_digest, '"l2"', '"p2"', None, False)
        manager = state_sync.StateSyncManager(self.home)
        with mock.patch.object(manager, "_remote_head", side_effect=[first, second]) as remote_head:
            manager.prepare_turn("token")
            manager.prepare_turn("token")

        self.assertEqual(2, remote_head.call_count)
        self.assertEqual(
            "remote update", (self.home / "memories" / "MEMORY.md").read_text())

    def test_unchanged_remote_head_uses_metadata_only(self):
        manager = state_sync.StateSyncManager(self.home)
        manager._prepared = True
        manager._write_blocked = False
        manager._legacy_etag = '"legacy"'
        manager._pointer_etag = '"pointer"'
        manager._uploaded_hash = "a" * 64
        legacy = state_sync.RemoteArchive(
            '"legacy"', 100, "https://tenant.sharepoint.com/l")
        pointer = state_sync.RemoteArchive(
            '"pointer"', 100, "https://tenant.sharepoint.com/p")
        with mock.patch.object(manager, "_metadata_name", return_value=pointer), \
                mock.patch.object(manager, "_metadata", return_value=legacy), \
                mock.patch.object(manager, "_download") as download:
            head = manager._remote_head("token", self.config)

        download.assert_not_called()
        self.assertEqual(b"", head.content)
        self.assertFalse(head.needs_pointer)

    def test_failed_refresh_blocks_upload_from_unknown_base(self):
        manager = state_sync.StateSyncManager(self.home)
        with mock.patch.object(
            manager, "_remote_head", side_effect=state_sync.StateSyncError("future schema"),
        ):
            with self.assertRaises(state_sync.StateSyncError):
                manager.prepare_turn("token")
        with mock.patch.object(manager, "_upload") as upload:
            manager.finish_turn("token")
        upload.assert_not_called()

    def test_disabling_sync_gets_one_final_checkpoint_and_then_stops(self):
        manager = state_sync.StateSyncManager(self.home)
        with mock.patch.object(manager, "_remote_head", return_value=None):
            manager.prepare_turn("token")
        state_sync.write_config(self.home, {**self.config, "enabled": False})

        with mock.patch.object(manager, "_sync_once") as sync:
            manager.finish_turn("token")
            manager.finish_turn("token")

        self.assertEqual(1, sync.call_count)

    def test_older_writer_omission_does_not_delete_new_default_root(self):
        new_config = state_sync.write_config(self.home, {
            **self.config,
            "include_paths": [*self.config["include_paths"], "new-runtime-state"],
        })
        (self.home / "new-runtime-state").mkdir()
        durable = self.home / "new-runtime-state" / "data.json"
        durable.write_text("keep")
        base_content, _digest, _ = state_sync.build_archive(self.home, new_config)

        old_home = pathlib.Path(self.temp.name) / "old-writer"
        old_home.mkdir()
        old_config = state_sync.write_config(old_home, self.config)
        remote_content, _digest, _ = state_sync.build_archive(old_home, old_config)

        manager = state_sync.StateSyncManager(self.home)
        manager._base_hashes = state_sync._read_archive(base_content, new_config).hashes
        remote_archive = state_sync._read_archive(remote_content, new_config)
        manager._merge_remote_into_local(remote_archive)

        self.assertEqual("keep", durable.read_text())

    def test_pointer_rejects_unknown_state_or_component_schema(self):
        digest = "a" * 64
        pointer = {
            "format": 2,
            "state_schema": 2,
            "component_schemas": dict(state_sync.COMPONENT_SCHEMAS),
            "snapshot": {
                "name": f"{state_sync.SNAPSHOT_PREFIX}{digest}.tar.gz",
                "sha256": digest,
                "size": 123,
            },
            "history": [],
            "legacy_etag": '"l1"',
            "writer": {"release": "future", "protocol": "v3"},
        }
        with self.assertRaises(state_sync.StateSyncError):
            state_sync.StateSyncManager._validate_pointer(pointer)
        pointer["state_schema"] = 1
        pointer["component_schemas"] = {**state_sync.COMPONENT_SCHEMAS, "memory": 2}
        with self.assertRaises(state_sync.StateSyncError):
            state_sync.StateSyncManager._validate_pointer(pointer)

    def test_publish_writes_immutable_snapshot_before_pointer(self):
        manager = state_sync.StateSyncManager(self.home)
        manager._legacy_etag = '"legacy"'
        content, digest, _ = state_sync.build_archive(self.home, self.config)
        with mock.patch.object(
            manager, "_upload_name", side_effect=['"snapshot"', '"pointer"'],
        ) as upload:
            self.assertTrue(manager._publish_pointer("token", content, digest))

        self.assertEqual(f"{state_sync.SNAPSHOT_PREFIX}{digest}.tar.gz", upload.call_args_list[0].args[1])
        self.assertEqual(state_sync.POINTER_NAME, upload.call_args_list[1].args[1])
        pointer = json.loads(upload.call_args_list[1].args[2])
        self.assertEqual('"legacy"', pointer["legacy_etag"])
        self.assertEqual(digest, pointer["snapshot"]["sha256"])

    def test_pointer_conflict_retries_only_when_legacy_head_is_still_ours(self):
        manager = state_sync.StateSyncManager(self.home)
        manager._legacy_etag = '"legacy-ours"'
        manager._pointer_etag = '"pointer-old"'
        content, digest, _ = state_sync.build_archive(self.home, self.config)
        snapshot = manager._snapshot_entry(content, digest)
        latest_digest = "b" * 64
        latest = {
            "format": 2,
            "state_schema": 1,
            "component_schemas": dict(state_sync.COMPONENT_SCHEMAS),
            "snapshot": {
                "name": f"{state_sync.SNAPSHOT_PREFIX}{latest_digest}.tar.gz",
                "sha256": latest_digest,
                "size": 10,
            },
            "history": [],
            "legacy_etag": '"legacy-ours"',
            "writer": {"release": "other", "protocol": "dual-v1-v2"},
        }
        pointer_remote = state_sync.RemoteArchive(
            '"pointer-latest"', 10, "https://tenant.sharepoint.com/p")
        with mock.patch.object(manager, "_ensure_snapshot", return_value=snapshot), \
                mock.patch.object(manager, "_upload_name", side_effect=[
                    state_sync.StateSyncConflict("race"), '"pointer-final"',
                ]) as upload, \
                mock.patch.object(manager, "_metadata_name", return_value=pointer_remote), \
                mock.patch.object(manager, "_read_pointer_document", return_value=latest), \
                mock.patch.object(manager, "_metadata", return_value=state_sync.RemoteArchive(
                    '"legacy-ours"', len(content), "https://tenant.sharepoint.com/l")):
            self.assertTrue(manager._publish_pointer("token", content, digest))

        self.assertEqual(2, upload.call_count)
        self.assertEqual('"pointer-latest"', upload.call_args_list[1].args[3])
        self.assertEqual('"pointer-final"', manager._pointer_etag)

    def test_pointer_conflict_does_not_point_backwards_after_new_legacy_writer(self):
        manager = state_sync.StateSyncManager(self.home)
        manager._legacy_etag = '"legacy-ours"'
        content, digest, _ = state_sync.build_archive(self.home, self.config)
        snapshot = manager._snapshot_entry(content, digest)
        latest = manager._pointer_document(snapshot)
        pointer_remote = state_sync.RemoteArchive(
            '"pointer-latest"', 10, "https://tenant.sharepoint.com/p")
        with mock.patch.object(manager, "_ensure_snapshot", return_value=snapshot), \
                mock.patch.object(
                    manager, "_upload_name", side_effect=state_sync.StateSyncConflict("race"),
                ) as upload, \
                mock.patch.object(manager, "_metadata_name", return_value=pointer_remote), \
                mock.patch.object(manager, "_read_pointer_document", return_value=latest), \
                mock.patch.object(manager, "_metadata", return_value=state_sync.RemoteArchive(
                    '"legacy-newer"', len(content), "https://tenant.sharepoint.com/l")):
            self.assertFalse(manager._publish_pointer("token", content, digest))

        self.assertEqual(1, upload.call_count)


if __name__ == "__main__":
    unittest.main()
