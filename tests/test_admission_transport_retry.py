"""A daemon cutover must not abandon a job already in its acquire hook."""
import unittest
from unittest.mock import MagicMock,patch

from lib import host_admission


class AdmissionTransportRetryTests(unittest.TestCase):
    def reply(self, raw):
        client=MagicMock()
        client.__enter__.return_value=client
        client.recv.return_value=raw
        return client

    def test_eof_and_partial_reply_are_retryable_transport_errors(self):
        for raw in (b'',b'{',b'{"accepted":',b'\xff'):
            with self.subTest(raw=raw),patch.object(host_admission.socket,'socket',return_value=self.reply(raw)):
                with self.assertRaises(OSError):
                    host_admission.request('acquire')

    def test_typed_orphan_and_server_error_keep_terminal_semantics(self):
        replies=((b'{"code":"orphaned-runner-hook","error":"gone"}',host_admission.OrphanRunnerHook),
                 (b'{"error":"invalid policy"}',RuntimeError))
        for raw,error in replies:
            with self.subTest(raw=raw),patch.object(host_admission.socket,'socket',return_value=self.reply(raw)):
                with self.assertRaises(error):
                    host_admission.request('acquire')

    def test_valid_denial_is_not_a_transport_error(self):
        with patch.object(host_admission.socket,'socket',return_value=self.reply(b'{"accepted":false}')):
            self.assertIs(host_admission.request('acquire'),False)


if __name__=='__main__':unittest.main()
