#!/usr/bin/env python3
import atexit
import os
import pathlib
import shutil
import socket
import tempfile
import unittest

# Every temporary file this file and what it runs make goes under one directory,
# removed at exit: the suite left hundreds of directories in /tmp, and a run's /tmp
# ran out of inodes. Once per process: a test file another one imports keeps it.
if "IL_TEST_TMP" not in os.environ:
    os.environ["IL_TEST_TMP"] = os.environ["TMPDIR"] = tempfile.mkdtemp(prefix="il-test-")
    os.environ["ISSUELOOPER_TMP"] = os.path.join(os.environ["IL_TEST_TMP"], "scratch")
    atexit.register(shutil.rmtree, os.environ["IL_TEST_TMP"], True)
tempfile.tempdir = os.environ["IL_TEST_TMP"]

from broker_socket import prepare_socket, socket_path


class SocketTest(unittest.TestCase):
    def test_short_path_stays_in_run_directory(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="sock-") as d:
            path = pathlib.Path(d) / "broker.sock"
            target = prepare_socket(path)
            self.assertEqual(target, path.parent.resolve() / path.name)
            self.assertFalse(path.is_symlink())


    def test_long_path_and_alias_connect_to_same_socket(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            run = root / ('long-checkout-' * 12) / '.issuelooper' / 'run'
            run.mkdir(parents=True)
            alias = root / 'alias'
            alias.symlink_to(run, target_is_directory=True)
            path = run / 'broker.sock'
            address = prepare_socket(path)
            try:
                self.assertLess(len(bytes(address)), 104)
                self.assertEqual(socket_path(alias / 'broker.sock'), address)
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(address))
                    server.listen(1)
                    with socket.socket(socket.AF_UNIX) as client:
                        client.settimeout(2)
                        client.connect(str(socket_path(path)))
                        peer, _ = server.accept()
                        with peer:
                            client.sendall(b'ok')
                            self.assertEqual(peer.recv(2), b'ok')
                self.assertTrue(path.is_socket())
            finally:
                address.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
