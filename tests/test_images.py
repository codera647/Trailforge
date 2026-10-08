from unittest.mock import patch
import unittest

from trailforge.adapters.images import resolve_image, select_image
from trailforge.core import ContractError


class ImageSelectionTests(unittest.TestCase):
    def test_exact_platform_manifest_is_selected_without_runtime_claim(self):
        data = {'images': [
            {'os': 'linux', 'architecture': 'amd64', 'digest': 'sha256:' + 'a' * 64, 'status': 'active'},
            {'os': 'linux', 'architecture': 'arm64', 'digest': 'sha256:' + 'b' * 64, 'status': 'active'}]}
        result = select_image(data, tag='3.13-slim-bookworm', architecture='amd64')
        self.assertEqual(result['image'], 'docker.io/library/python@sha256:' + 'a' * 64)
        self.assertFalse(result['image_pulled'])
        self.assertFalse(result['runtime_verified'])

    def test_missing_ambiguous_nonlinux_and_invalid_digest_are_denied(self):
        image = {'os': 'linux', 'architecture': 'amd64', 'digest': 'sha256:' + 'a' * 64, 'status': 'active'}
        for data in ({'images': []}, {'images': [image, image]}, {'images': [{**image, 'os': 'windows'}]},
                     {'images': [{**image, 'digest': 'mutable-tag'}]}, {'images': [{**image, 'status': 'inactive'}]}):
            with self.assertRaises(ContractError):
                select_image(data, tag='3.13-slim-bookworm', architecture='amd64')

    def test_untrusted_tag_cannot_choose_other_registry_or_url(self):
        with patch('trailforge.adapters.images.urlopen') as request:
            for tag in ('latest', '../../other', 'https://example.com', '3.13-slim-bookworm?extra=1', None, 3):
                with self.assertRaises(ContractError):
                    resolve_image(tag=tag)
                with self.assertRaises(ContractError):
                    select_image({'images': []}, tag=tag, architecture='amd64')
            request.assert_not_called()
