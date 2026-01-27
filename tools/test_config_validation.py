#!/usr/bin/env python3
"""
Test script for device configuration validation
"""
import json
import sys
from utils.config import validate_device_config, validate_all_devices

def test_valid_config():
    """Test a valid configuration"""
    print("\n=== Testing Valid Configuration ===")
    valid_config = {
        "router1": {
            "ip": "192.168.1.1",
            "port": 22,
            "username": "admin",
            "auth": {
                "type": "password",
                "password": "secret123"
            }
        }
    }
    
    try:
        validate_all_devices(valid_config)
        print("✅ Valid configuration passed validation")
    except ValueError as e:
        print(f"❌ Unexpected error: {e}")
        return False
    return True

def test_missing_ip():
    """Test configuration missing IP field"""
    print("\n=== Testing Missing IP Field ===")
    config = {
        "router1": {
            # Missing "ip" field
            "port": 22,
            "username": "admin",
            "auth": {
                "type": "password",
                "password": "secret123"
            }
        }
    }
    
    try:
        validate_all_devices(config)
        print("❌ Should have failed for missing IP")
        return False
    except ValueError as e:
        print(f"✅ Correctly caught error: {e}")
        return True

def test_missing_auth():
    """Test configuration missing authentication"""
    print("\n=== Testing Missing Authentication ===")
    config = {
        "router1": {
            "ip": "192.168.1.1",
            "port": 22,
            "username": "admin"
            # Missing auth section and password field
        }
    }
    
    try:
        validate_all_devices(config)
        print("❌ Should have failed for missing auth")
        return False
    except ValueError as e:
        print(f"✅ Correctly caught error: {e}")
        return True

def test_invalid_auth_type():
    """Test configuration with invalid auth type"""
    print("\n=== Testing Invalid Auth Type ===")
    config = {
        "router1": {
            "ip": "192.168.1.1",
            "port": 22,
            "username": "admin",
            "auth": {
                "type": "invalid_type",
                "password": "secret123"
            }
        }
    }
    
    try:
        validate_all_devices(config)
        print("❌ Should have failed for invalid auth type")
        return False
    except ValueError as e:
        print(f"✅ Correctly caught error: {e}")
        return True

def test_ssh_key_missing_path():
    """Test SSH key auth missing private_key_path"""
    print("\n=== Testing SSH Key Auth Missing Path ===")
    config = {
        "router1": {
            "ip": "192.168.1.1",
            "port": 22,
            "username": "admin",
            "auth": {
                "type": "ssh_key"
                # Missing private_key_path
            }
        }
    }
    
    try:
        validate_all_devices(config)
        print("❌ Should have failed for missing SSH key path")
        return False
    except ValueError as e:
        print(f"✅ Correctly caught error: {e}")
        return True

def test_invalid_port_type():
    """Test configuration with invalid port type"""
    print("\n=== Testing Invalid Port Type ===")
    config = {
        "router1": {
            "ip": "192.168.1.1",
            "port": "22",  # Should be int, not string
            "username": "admin",
            "auth": {
                "type": "password",
                "password": "secret123"
            }
        }
    }
    
    try:
        validate_all_devices(config)
        print("❌ Should have failed for invalid port type")
        return False
    except ValueError as e:
        print(f"✅ Correctly caught error: {e}")
        return True

def test_backward_compatibility():
    """Test backward compatibility with old password format"""
    print("\n=== Testing Backward Compatibility ===")
    config = {
        "router1": {
            "ip": "192.168.1.1",
            "port": 22,
            "username": "admin",
            "password": "secret123"  # Old format
        }
    }
    
    try:
        validate_all_devices(config)
        print("✅ Backward compatible configuration passed validation")
    except ValueError as e:
        print(f"❌ Unexpected error: {e}")
        return False
    return True

def test_multiple_devices():
    """Test multiple devices with mixed valid and invalid configs"""
    print("\n=== Testing Multiple Devices ===")
    config = {
        "router1": {
            "ip": "192.168.1.1",
            "port": 22,
            "username": "admin",
            "auth": {
                "type": "password",
                "password": "secret123"
            }
        },
        "router2": {
            # Missing IP
            "port": 22,
            "username": "admin",
            "auth": {
                "type": "ssh_key",
                "private_key_path": "/path/to/key"
            }
        },
        "router3": {
            "ip": "192.168.1.3",
            "port": "invalid",  # Invalid port type
            "username": "admin",
            "password": "secret"
        }
    }
    
    try:
        validate_all_devices(config)
        print("❌ Should have failed for invalid devices")
        return False
    except ValueError as e:
        error_str = str(e)
        print(f"✅ Correctly caught errors:\n{error_str}")
        # Check that both errors are reported
        if "router2" in error_str and "router3" in error_str:
            print("✅ All invalid devices reported")
            return True
        else:
            print("❌ Not all invalid devices were reported")
            return False

def main():
    """Run all tests"""
    print("=" * 60)
    print("Device Configuration Validation Tests")
    print("=" * 60)
    
    tests = [
        test_valid_config,
        test_missing_ip,
        test_missing_auth,
        test_invalid_auth_type,
        test_ssh_key_missing_path,
        test_invalid_port_type,
        test_backward_compatibility,
        test_multiple_devices
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        if test():
            passed += 1
        else:
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"Test Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())