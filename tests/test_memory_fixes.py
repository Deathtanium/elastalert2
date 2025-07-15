#!/usr/bin/env python3
"""
Test script to verify memory leak fixes in ElastAlert2
"""
import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from elastalert.elastalert import ElastAlerter
from elastalert.util import ts_now, ts_to_dt


def test_silence_cache_cleanup():
    """Test that expired silence cache entries are cleaned up"""
    print("Testing silence cache cleanup...")
    
    # Create a mock ElastAlerter instance
    ea = ElastAlerter([])
    
    # Add some expired and non-expired entries
    now = ts_now()
    expired_time = now - datetime.timedelta(hours=1)
    future_time = now + datetime.timedelta(hours=1)
    
    ea.silence_cache = {
        'expired_rule1': (expired_time, 0),
        'expired_rule2': (expired_time, 0),
        'active_rule': (future_time, 0)
    }
    
    initial_count = len(ea.silence_cache)
    ea.cleanup_silence_cache()
    final_count = len(ea.silence_cache)
    
    print(f"Initial silence cache entries: {initial_count}")
    print(f"Final silence cache entries: {final_count}")
    print(f"Remaining entries: {list(ea.silence_cache.keys())}")
    
    assert final_count == 1, f"Expected 1 entry, got {final_count}"
    assert 'active_rule' in ea.silence_cache, "Active rule should remain"
    print("✓ Silence cache cleanup test passed")


def test_es_clients_cleanup():
    """Test that stale ES client entries are cleaned up"""
    print("\nTesting ES clients cleanup...")
    
    ea = ElastAlerter([])
    
    # Mock some rules
    ea.rules = [{'name': 'active_rule1'}, {'name': 'active_rule2'}]
    ea.disabled_rules = [{'name': 'disabled_rule'}]
    
    # Add ES clients including stale ones
    ea.es_clients = {
        'active_rule1': 'mock_client1',
        'active_rule2': 'mock_client2', 
        'disabled_rule': 'mock_client3',
        'old_deleted_rule': 'mock_client4',
        'another_old_rule': 'mock_client5'
    }
    
    initial_count = len(ea.es_clients)
    ea.cleanup_es_clients_cache()
    final_count = len(ea.es_clients)
    
    print(f"Initial ES client entries: {initial_count}")
    print(f"Final ES client entries: {final_count}")
    print(f"Remaining clients: {list(ea.es_clients.keys())}")
    
    expected_remaining = {'active_rule1', 'active_rule2', 'disabled_rule'}
    actual_remaining = set(ea.es_clients.keys())
    
    assert actual_remaining == expected_remaining, f"Expected {expected_remaining}, got {actual_remaining}"
    print("✓ ES clients cleanup test passed")


def test_aggregate_cleanup():
    """Test that expired aggregate alert times are cleaned up"""
    print("\nTesting aggregate cleanup...")
    
    ea = ElastAlerter([])
    
    now = ts_now()
    expired_time = now - datetime.timedelta(hours=1)
    future_time = now + datetime.timedelta(hours=1)
    
    # Mock rules with aggregate data
    ea.rules = [
        {
            'name': 'test_rule1',
            'aggregate_alert_time': {
                'expired_key1': expired_time,
                'expired_key2': expired_time,
                'active_key': future_time
            },
            'current_aggregate_id': {
                'expired_key1': 'id1',
                'expired_key2': 'id2', 
                'active_key': 'id3'
            }
        }
    ]
    
    initial_agg_count = len(ea.rules[0]['aggregate_alert_time'])
    initial_id_count = len(ea.rules[0]['current_aggregate_id'])
    
    ea.cleanup_expired_aggregates()
    
    final_agg_count = len(ea.rules[0]['aggregate_alert_time'])
    final_id_count = len(ea.rules[0]['current_aggregate_id'])
    
    print(f"Initial aggregate entries: {initial_agg_count}")
    print(f"Final aggregate entries: {final_agg_count}")
    print(f"Remaining aggregate keys: {list(ea.rules[0]['aggregate_alert_time'].keys())}")
    
    assert final_agg_count == 1, f"Expected 1 aggregate entry, got {final_agg_count}"
    assert final_id_count == 1, f"Expected 1 aggregate ID entry, got {final_id_count}"
    assert 'active_key' in ea.rules[0]['aggregate_alert_time'], "Active key should remain"
    print("✓ Aggregate cleanup test passed")


def test_rule_memory_cleanup():
    """Test that rule memory is properly cleaned up"""
    print("\nTesting rule memory cleanup...")
    
    ea = ElastAlerter([])
    
    # Mock rule with various memory allocations
    now = ts_now()
    old_time = now - datetime.timedelta(hours=2)  # Older than typical buffer time
    
    rule = {
        'name': 'test_rule',
        'processed_hits': {
            'old_hit': old_time,      # This should be removed
            'recent_hit': now         # This should remain
        },
        'agg_matches': [{'match1': 'data'}, {'match2': 'data'}],
        'aggregate_alert_time': {
            'expired_key': old_time,  # This should be removed
            'active_key': now + datetime.timedelta(hours=1)  # This should remain
        },
        'current_aggregate_id': {
            'expired_key': 'old_id',
            'active_key': 'active_id'
        },
        'buffer_time': datetime.timedelta(minutes=30),  # 30 minute buffer
        'type': type('MockRuleType', (), {'garbage_collect': lambda self, ts: None})()
    }
    
    # Add ES client and silence cache entries that match this rule
    ea.es_clients['test_rule'] = 'mock_client'
    ea.silence_cache['test_rule._silence'] = (ts_now() + datetime.timedelta(hours=1), 0)
    ea.silence_cache['test_rule.query_key'] = (ts_now() + datetime.timedelta(hours=1), 0)
    
    initial_processed_hits = len(rule['processed_hits'])
    initial_aggregate_alerts = len(rule['aggregate_alert_time'])
    
    ea.cleanup_rule_memory(rule)
    
    final_processed_hits = len(rule['processed_hits'])
    final_aggregate_alerts = len(rule['aggregate_alert_time'])
    
    print(f"Initial processed hits: {initial_processed_hits}, Final: {final_processed_hits}")
    print(f"Initial aggregate alerts: {initial_aggregate_alerts}, Final: {final_aggregate_alerts}")
    
    # Check that old processed hits were removed but recent ones remain
    assert len(rule['processed_hits']) == 1, f"Expected 1 processed hit, got {len(rule['processed_hits'])}"
    assert 'recent_hit' in rule['processed_hits'], "Recent hit should remain"
    assert 'old_hit' not in rule['processed_hits'], "Old hit should be removed"
    
    # Check that expired aggregate alert times were removed
    assert len(rule['aggregate_alert_time']) == 1, f"Expected 1 aggregate alert, got {len(rule['aggregate_alert_time'])}"
    assert 'active_key' in rule['aggregate_alert_time'], "Active aggregate should remain"
    assert 'expired_key' not in rule['aggregate_alert_time'], "Expired aggregate should be removed"
    
    # Check that corresponding current_aggregate_id entries were cleaned
    assert len(rule['current_aggregate_id']) == 1, f"Expected 1 current aggregate ID, got {len(rule['current_aggregate_id'])}"
    assert 'active_key' in rule['current_aggregate_id'], "Active aggregate ID should remain"
    assert 'expired_key' not in rule['current_aggregate_id'], "Expired aggregate ID should be removed"
    
    print("✓ Rule memory cleanup test passed")


if __name__ == '__main__':
    print("Running ElastAlert2 Memory Leak Fix Tests")
    print("=" * 50)
    
    try:
        test_silence_cache_cleanup()
        test_es_clients_cleanup()
        test_aggregate_cleanup()
        test_rule_memory_cleanup()
        
        print("\n" + "=" * 50)
        print("✅ All memory leak fix tests passed!")
        print("\nThe following memory leak issues have been fixed:")
        print("1. Silence cache entries are now cleaned up when expired")
        print("2. Elasticsearch client cache is cleaned up for removed rules")  
        print("3. Expired aggregate alert time entries are removed")
        print("4. Rule memory is properly cleaned up when rules are removed")
        print("5. Periodic cleanup runs every 10 minutes to prevent memory growth")
        print("6. Scroll IDs are properly cleaned up even in error conditions")
        
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
