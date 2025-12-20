"""
Unit tests for UtilitiesExtension utility.
"""
import pytest
import hmac
import hashlib
from utils.extensions.utilities_extention import UtilitiesExtension


@pytest.mark.unit
class TestUtilitiesExtension:
    """Test cases for UtilitiesExtension class."""
    
    def test_init(self, utilities_extension):
        """Test UtilitiesExtension initialization."""
        assert utilities_extension.key == "test-secret-key-for-encryption-12345"
    
    def test_generate_time_based_uid(self, utilities_extension):
        """Test time-based UID generation."""
        uid = utilities_extension.generate_time_based_uid()
        assert isinstance(uid, str)
        assert len(uid) == 16
        # Should be alphanumeric with underscores
        assert all(c.isalnum() or c == '_' for c in uid)
    
    def test_generate_time_based_uid_uniqueness(self, utilities_extension):
        """Test that time-based UIDs are unique."""
        uid1 = utilities_extension.generate_time_based_uid()
        import time
        time.sleep(0.001)  # Small delay to ensure different timestamp
        uid2 = utilities_extension.generate_time_based_uid()
        assert uid1 != uid2
    
    def test_generate_uuid_with_key(self, utilities_extension):
        """Test UUID generation with key."""
        uuid_str = utilities_extension.generate_uuid_with_key()
        assert isinstance(uuid_str, str)
        assert len(uuid_str) > 0
    
    def test_generate_uuid_with_key_consistency(self, utilities_extension):
        """Test that UUID with key is consistent."""
        uuid1 = utilities_extension.generate_uuid_with_key()
        uuid2 = utilities_extension.generate_uuid_with_key()
        assert uuid1 == uuid2  # Same key should produce same UUID
    
    def test_encode_phrase_with_key(self, utilities_extension):
        """Test phrase encoding with key."""
        phrase = "test-phrase"
        encoded = utilities_extension.encode_phrase_with_key(phrase)
        assert isinstance(encoded, str)
        assert len(encoded) == 48  # Default size
    
    def test_encode_phrase_with_key_custom_size(self, utilities_extension):
        """Test phrase encoding with custom size."""
        phrase = "test-phrase"
        encoded = utilities_extension.encode_phrase_with_key(phrase, size=32)
        assert len(encoded) == 32
    
    def test_encode_phrase_with_key_none(self, utilities_extension):
        """Test phrase encoding with None phrase."""
        encoded = utilities_extension.encode_phrase_with_key(None)
        assert encoded is None
    
    def test_encode_phrase_with_key_consistency(self, utilities_extension):
        """Test that phrase encoding is consistent."""
        phrase = "test-phrase"
        encoded1 = utilities_extension.encode_phrase_with_key(phrase)
        encoded2 = utilities_extension.encode_phrase_with_key(phrase)
        assert encoded1 == encoded2
    
    def test_encode_phrase_with_key_different_phrases(self, utilities_extension):
        """Test that different phrases produce different encodings."""
        phrase1 = "test-phrase-1"
        phrase2 = "test-phrase-2"
        encoded1 = utilities_extension.encode_phrase_with_key(phrase1)
        encoded2 = utilities_extension.encode_phrase_with_key(phrase2)
        assert encoded1 != encoded2
    
    def test_encode_hostname_with_key(self, utilities_extension):
        """Test hostname encoding with key."""
        hostname = "test-host"
        encoded = utilities_extension.encode_hostname_with_key(hostname)
        assert isinstance(encoded, str)
        assert len(encoded) == 48  # Default size
    
    def test_encode_hostname_with_key_default(self, utilities_extension):
        """Test hostname encoding with default (None) hostname."""
        encoded = utilities_extension.encode_hostname_with_key(None)
        assert isinstance(encoded, str)
        assert len(encoded) == 48
    
    def test_encode_hostname_with_key_custom_size(self, utilities_extension):
        """Test hostname encoding with custom size."""
        hostname = "test-host"
        encoded = utilities_extension.encode_hostname_with_key(hostname, size=64)
        assert len(encoded) == 64
    
    def test_encode_hostname_with_key_consistency(self, utilities_extension):
        """Test that hostname encoding is consistent."""
        hostname = "test-host"
        encoded1 = utilities_extension.encode_hostname_with_key(hostname)
        encoded2 = utilities_extension.encode_hostname_with_key(hostname)
        assert encoded1 == encoded2
    
    def test_encode_hostname_with_key_different_hostnames(self, utilities_extension):
        """Test that different hostnames produce different encodings."""
        hostname1 = "host1"
        hostname2 = "host2"
        encoded1 = utilities_extension.encode_hostname_with_key(hostname1)
        encoded2 = utilities_extension.encode_hostname_with_key(hostname2)
        assert encoded1 != encoded2
    
    def test_encode_hostname_with_key_hmac_algorithm(self, utilities_extension):
        """Test hostname encoding with different hash algorithms."""
        hostname = "test-host"
        encoded_sha256 = utilities_extension.encode_hostname_with_key(hostname, hash_algorithm='sha256')
        encoded_sha512 = utilities_extension.encode_hostname_with_key(hostname, hash_algorithm='sha512')
        assert encoded_sha256 != encoded_sha512
        assert len(encoded_sha256) == 48
        assert len(encoded_sha512) == 48
    
    def test_singleton_pattern(self, utilities_extension):
        """Test that UtilitiesExtension follows singleton pattern."""
        key = "test-secret-key-for-encryption-12345"
        ext1 = UtilitiesExtension(key)
        ext2 = UtilitiesExtension(key)
        assert ext1 is ext2

