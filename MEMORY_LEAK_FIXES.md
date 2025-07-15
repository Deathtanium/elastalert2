# ElastAlert2 Memory Leak Fixes

This document describes the memory leak issues identified and fixed in the ElastAlert2 codebase.

## Issues Identified and Fixed

### 1. Silence Cache Memory Leak
**Problem**: The `silence_cache` dictionary in `ElastAlerter` class never removed expired entries, causing unbounded memory growth over time.

**Location**: `elastalert/elastalert.py` - `is_silenced()` method

**Fix**: 
- Modified `is_silenced()` method to remove expired entries when checking cache
- Added `cleanup_silence_cache()` method for periodic cleanup
- Scheduled periodic cleanup every 10 minutes

**Code Changes**:
```python
def is_silenced(self, rule_name):
    if rule_name in self.silence_cache:
        if ts_now() < self.silence_cache[rule_name][0]:
            return True
        else:
            # Remove expired silence cache entry
            self.silence_cache.pop(rule_name, None)
```

### 2. Elasticsearch Client Cache Memory Leak
**Problem**: The `es_clients` dictionary accumulated client references for rules that no longer existed, never cleaning them up.

**Location**: `elastalert/elastalert.py` - `get_elasticsearch_client()` method

**Fix**:
- Added `cleanup_es_clients_cache()` method to remove clients for non-existent rules
- Integrated cleanup into periodic maintenance routine
- Added rule-specific cleanup when rules are removed

**Code Changes**:
```python
def cleanup_es_clients_cache(self):
    active_rule_names = {rule['name'] for rule in self.rules}
    active_rule_names.update({rule['name'] for rule in self.disabled_rules})
    
    stale_clients = []
    for client_key in self.es_clients.keys():
        if client_key not in active_rule_names:
            stale_clients.append(client_key)
    
    for key in stale_clients:
        self.es_clients.pop(key, None)
```

### 3. Aggregate Alert Time Memory Leak
**Problem**: The `aggregate_alert_time` dictionary in rules could accumulate expired entries without cleanup.

**Location**: Rule-level aggregate data management

**Fix**:
- Added `cleanup_expired_aggregates()` method to remove expired aggregate alert times
- Synchronized cleanup of both `aggregate_alert_time` and `current_aggregate_id` dictionaries
- Integrated into periodic cleanup routine

**Code Changes**:
```python
def cleanup_expired_aggregates(self):
    now = ts_now()
    cleaned_count = 0
    
    for rule in self.rules:
        if 'aggregate_alert_time' in rule:
            expired_keys = []
            for agg_key, alert_time in rule['aggregate_alert_time'].items():
                if now > alert_time:
                    expired_keys.append(agg_key)
            
            for key in expired_keys:
                rule['aggregate_alert_time'].pop(key, None)
                rule['current_aggregate_id'].pop(key, None)
                cleaned_count += 1
```

### 4. Scroll ID Memory Leak
**Problem**: Elasticsearch scroll IDs were not always properly cleaned up in error conditions, potentially causing memory leaks on the Elasticsearch side.

**Location**: `elastalert/elastalert.py` - `run_query()` method

**Fix**:
- Added `cleanup_scroll_id()` method for safe scroll ID cleanup
- Added finally block to ensure scroll IDs are always cleaned up
- Added error handling for cases where ES client is not available

**Code Changes**:
```python
def cleanup_scroll_id(self, rule):
    if 'scroll_id' in rule:
        scroll_id = rule.pop('scroll_id')
        try:
            if hasattr(self.thread_data, 'current_es') and self.thread_data.current_es:
                self.thread_data.current_es.clear_scroll(scroll_id=scroll_id)
        except (NotFoundError, AttributeError, ElasticsearchException):
            pass

# In run_query method:
try:
    # ... scrolling logic ...
finally:
    self.cleanup_scroll_id(rule)
```

### 5. Rule Memory Cleanup
**Problem**: When rules were deleted or disabled, their associated memory (processed_hits, aggregation data, etc.) was not properly cleaned up.

**Location**: Rule lifecycle management

**Fix**:
- Added `cleanup_rule_memory()` method to comprehensively clean rule-associated memory
- Integrated cleanup into rule deletion and disabling logic
- Clears processed_hits, aggregation data, and associated caches

**Code Changes**:
```python
def cleanup_rule_memory(self, rule):
    rule_name = rule['name']
    
    # Clear rule-specific caches
    if 'processed_hits' in rule:
        rule['processed_hits'].clear()
    if 'agg_matches' in rule:
        rule['agg_matches'].clear()
    if 'aggregate_alert_time' in rule:
        rule['aggregate_alert_time'].clear()
    if 'current_aggregate_id' in rule:
        rule['current_aggregate_id'].clear()
    
    # Clear ES client and silence entries
    self.es_clients.pop(rule_name, None)
    silence_keys = [key for key in self.silence_cache.keys() if key.startswith(rule_name)]
    for key in silence_keys:
        self.silence_cache.pop(key, None)
```

### 6. Periodic Memory Maintenance
**Problem**: No periodic cleanup mechanism existed to prevent gradual memory accumulation.

**Fix**:
- Added comprehensive `cleanup_memory_caches()` method
- Scheduled periodic cleanup every 10 minutes using the existing scheduler
- Includes cleanup of all identified memory leak sources

**Code Changes**:
```python
def cleanup_memory_caches(self):
    self.cleanup_silence_cache()
    self.cleanup_es_clients_cache()
    self.cleanup_expired_aggregates()
    # Clean up old processed hits for all rules
    for rule in self.rules:
        self.remove_old_events(rule)

# In start() method:
self.scheduler.add_job(self.cleanup_memory_caches, 'interval',
                      seconds=600,  # 10 minutes
                      id='_internal_memory_cleanup',
                      name='Internal: Memory Cache Cleanup')
```

## Impact Assessment

### Before Fixes:
- Memory usage would grow indefinitely over time
- Long-running ElastAlert2 instances would eventually consume excessive memory
- Elasticsearch scroll contexts might not be properly cleaned up
- Rule deletion/modification could leave stale data in memory

### After Fixes:
- Memory usage remains bounded even in long-running instances
- Expired cache entries are automatically cleaned up
- Elasticsearch scroll contexts are properly managed
- Rule lifecycle changes properly clean up associated memory
- Periodic maintenance prevents gradual memory accumulation

## Monitoring and Verification

To verify the fixes are working:

1. **Monitor Memory Usage**: Track ElastAlert2 process memory usage over time
2. **Log Analysis**: Look for debug messages about cache cleanup operations
3. **Elasticsearch Monitoring**: Monitor scroll context usage on ES cluster
4. **Rule Management**: Verify memory is cleaned up when rules are added/removed

## Configuration

The memory cleanup is automatic and requires no configuration changes. The cleanup runs every 10 minutes by default. If needed, you can modify the cleanup interval by changing the `seconds` parameter in the scheduler job setup.

## Testing

The fixes include comprehensive test coverage to verify:
- Silence cache cleanup removes expired entries
- ES client cache cleanup removes stale clients
- Aggregate cleanup removes expired aggregation data
- Rule memory cleanup comprehensively clears rule data
- Periodic maintenance runs without errors

## Backward Compatibility

All fixes are backward compatible and do not change the public API or configuration format. Existing ElastAlert2 deployments will benefit from these fixes without any required changes.
