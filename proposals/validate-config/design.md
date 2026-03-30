# Feature: Config Validation RPC

## Overview

Add a RESTCONF RPC endpoint that validates a `config_db.json` payload against all loaded YANG models without applying it. Returns a comprehensive list of all validation errors, not just the first one.

## Motivation

Currently there is no way to validate a configuration before applying it. `config replace` validates inline during application, and failures are reported one at a time as each table is processed. Operators managing SONiC at scale (via NAPALM, Ansible, NetBox, or custom automation) need a way to pre-validate configuration changes before committing them to the running system.

## Requirements

1. Accept a full or partial `config_db.json` payload via RESTCONF RPC
2. Validate against all loaded YANG models (CVL)
3. Collect **all** validation errors, not fail-fast on the first one
4. Return a structured response with table, key, field, and constraint details for each error
5. No side effects — do not modify running config or config_db

## API

### RESTCONF RPC

```
POST /restconf/operations/sonic-config-mgmt:validate-config
Content-Type: application/yang-data+json

{
  "sonic-config-mgmt:input": {
    "config-db-json": "{ ... full config_db.json as escaped string ... }"
  }
}
```

### Response (valid)

```json
{
  "sonic-config-mgmt:output": {
    "valid": true,
    "error-count": 0,
    "errors": []
  }
}
```

### Response (invalid)

```json
{
  "sonic-config-mgmt:output": {
    "valid": false,
    "error-count": 3,
    "errors": [
      {
        "table": "BGP_NEIGHBOR",
        "key": "Vrf_red|10.1.0.1",
        "field": "local_addr",
        "error-code": "SEMANTIC_DEPENDENT_DATA_MISSING",
        "error-message": "Referenced interface IP does not exist",
        "constraint": "leafref validation"
      },
      {
        "table": "VLAN_MEMBER",
        "key": "Vlan100|Ethernet99",
        "field": "ifname",
        "error-code": "SEMANTIC_KEY_NOT_EXIST",
        "error-message": "Referenced port Ethernet99 does not exist in PORT table",
        "constraint": "leafref to PORT"
      },
      {
        "table": "VXLAN_TUNNEL_MAP",
        "key": "vtep1|map_10100_Vlan200",
        "field": "vlan",
        "error-code": "SEMANTIC_DEPENDENT_DATA_MISSING",
        "error-message": "Vlan200 does not exist in VLAN table",
        "constraint": "leafref to VLAN"
      }
    ]
  }
}
```

### CLI

```
admin@switch:~$ sudo config validate /tmp/candidate_config.json
Validating configuration...

ERROR [1/3]: BGP_NEIGHBOR|Vrf_red|10.1.0.1
  Field: local_addr
  Referenced interface IP does not exist (leafref validation)

ERROR [2/3]: VLAN_MEMBER|Vlan100|Ethernet99
  Field: ifname
  Referenced port Ethernet99 does not exist in PORT table (leafref to PORT)

ERROR [3/3]: VXLAN_TUNNEL_MAP|vtep1|map_10100_Vlan200
  Field: vlan
  Vlan200 does not exist in VLAN table (leafref to VLAN)

Validation failed with 3 errors.
```

```
admin@switch:~$ sudo config validate /tmp/good_config.json
Validating configuration...
Configuration is valid.
```

## Implementation

### Files Modified

1. `src/sonic-mgmt-common/models/yang/sonic/sonic-config-validate.yang` (new)
2. `src/sonic-mgmt-common/translib/transformer/xfmr_config_validate.go` (new)
3. `src/sonic-mgmt-common/cvl/cvl_api.go` (add `ValidateConfig` method)
4. `src/sonic-utilities/config/main.py` (add `validate` subcommand)

### CVL Changes

The key change is adding a `ValidateConfig()` method to CVL that iterates all tables/entries in the candidate config and collects all errors instead of returning on the first failure:

```go
func (c *CVL) ValidateConfig(configData map[string]interface{}) []CVLErrorInfo {
    var errors []CVLErrorInfo
    for tableName, tableData := range configData {
        for key, fields := range tableData.(map[string]interface{}) {
            editData := cmn.CVLEditConfigData{
                VType:     cmn.VALIDATE_ALL,
                VOp:       cmn.OP_CREATE,
                Key:       key,
                Data:      fields,
                TableName: tableName,
            }
            err, ret := c.ValidateEditConfig([]cmn.CVLEditConfigData{editData})
            if ret != CVL_SUCCESS {
                errors = append(errors, err)
            }
        }
    }
    return errors
}
```
