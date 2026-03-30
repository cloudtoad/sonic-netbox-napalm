////////////////////////////////////////////////////////////////////////////////
//                                                                            //
//  Copyright 2026 SONiC Contributors                                         //
//                                                                            //
//  Licensed under the Apache License, Version 2.0 (the "License");           //
//  you may not use this file except in compliance with the License.          //
//  You may obtain a copy of the License at                                   //
//                                                                            //
//  http://www.apache.org/licenses/LICENSE-2.0                                //
//                                                                            //
//  Unless required by applicable law or agreed to in writing, software       //
//  distributed under the License is distributed on an "AS IS" BASIS,         //
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  //
//  See the License for the specific language governing permissions and       //
//  limitations under the License.                                            //
//                                                                            //
////////////////////////////////////////////////////////////////////////////////

package transformer

import (
	"encoding/json"
	"fmt"

	"github.com/Azure/sonic-mgmt-common/cvl"
	cmn "github.com/Azure/sonic-mgmt-common/cvl/common"
	"github.com/Azure/sonic-mgmt-common/translib/db"
	"github.com/golang/glog"
)

func init() {
	XlateFuncBind("rpc_validate_config_cb", rpc_validate_config_cb)
}

// ValidationError represents a single validation error for JSON output.
type ValidationError struct {
	Index        uint32 `json:"index"`
	TableName    string `json:"table-name"`
	Key          string `json:"key"`
	Field        string `json:"field"`
	ErrorCode    string `json:"error-code"`
	ErrorMessage string `json:"error-message"`
	Constraint   string `json:"constraint"`
}

// errorCodeToString maps CVL error codes to human-readable strings.
func errorCodeToString(code cvl.CVLRetCode) string {
	switch code {
	case cvl.CVL_SYNTAX_ERROR:
		return "SYNTAX_ERROR"
	case cvl.CVL_SEMANTIC_ERROR:
		return "SEMANTIC_ERROR"
	case cvl.CVL_SYNTAX_MISSING_FIELD:
		return "SYNTAX_MISSING_FIELD"
	case cvl.CVL_SYNTAX_INVALID_FIELD:
		return "SYNTAX_INVALID_FIELD"
	case cvl.CVL_SYNTAX_INVALID_INPUT_DATA:
		return "SYNTAX_INVALID_INPUT_DATA"
	case cvl.CVL_SYNTAX_MULTIPLE_INSTANCE:
		return "SYNTAX_MULTIPLE_INSTANCE"
	case cvl.CVL_SYNTAX_DUPLICATE:
		return "SYNTAX_DUPLICATE"
	case cvl.CVL_SYNTAX_ENUM_INVALID:
		return "SYNTAX_ENUM_INVALID"
	case cvl.CVL_SYNTAX_ENUM_INVALID_NAME:
		return "SYNTAX_ENUM_INVALID_NAME"
	case cvl.CVL_SYNTAX_ENUM_WHITESPACE:
		return "SYNTAX_ENUM_WHITESPACE"
	case cvl.CVL_SYNTAX_OUT_OF_RANGE:
		return "SYNTAX_OUT_OF_RANGE"
	case cvl.CVL_SYNTAX_MINIMUM_INVALID:
		return "SYNTAX_MINIMUM_INVALID"
	case cvl.CVL_SYNTAX_MAXIMUM_INVALID:
		return "SYNTAX_MAXIMUM_INVALID"
	case cvl.CVL_SYNTAX_PATTERN_INVALID:
		return "SYNTAX_PATTERN_INVALID"
	case cvl.CVL_SEMANTIC_DEPENDENT_DATA_MISSING:
		return "SEMANTIC_DEPENDENT_DATA_MISSING"
	case cvl.CVL_SEMANTIC_MANDATORY_DATA_MISSING:
		return "SEMANTIC_MANDATORY_DATA_MISSING"
	case cvl.CVL_SEMANTIC_KEY_ALREADY_EXIST:
		return "SEMANTIC_KEY_ALREADY_EXIST"
	case cvl.CVL_SEMANTIC_KEY_NOT_EXIST:
		return "SEMANTIC_KEY_NOT_EXIST"
	case cvl.CVL_SEMANTIC_KEY_DUPLICATE:
		return "SEMANTIC_KEY_DUPLICATE"
	default:
		return fmt.Sprintf("ERROR_%d", code)
	}
}

// validateConfigDB validates a config_db.json payload against all YANG models
// and returns all errors found, not just the first one.
func validateConfigDB(configDBJson map[string]interface{}, dbs [db.MaxDB]*db.DB) []ValidationError {
	var errors []ValidationError
	var index uint32 = 0

	session, status := cvl.ValidationSessOpen(dbs[db.ConfigDB])
	if status != cvl.CVL_SUCCESS {
		errors = append(errors, ValidationError{
			Index:        1,
			ErrorCode:    "INTERNAL_ERROR",
			ErrorMessage: "Failed to open CVL validation session",
		})
		return errors
	}
	defer cvl.ValidationSessClose(session)

	// Iterate all tables and entries in the candidate config
	for tableName, tableDataRaw := range configDBJson {
		tableData, ok := tableDataRaw.(map[string]interface{})
		if !ok {
			index++
			errors = append(errors, ValidationError{
				Index:        index,
				TableName:    tableName,
				ErrorCode:    "SYNTAX_ERROR",
				ErrorMessage: fmt.Sprintf("Table %s: expected object, got %T", tableName, tableDataRaw),
			})
			continue
		}

		for key, fieldsRaw := range tableData {
			fieldsMap, ok := fieldsRaw.(map[string]interface{})
			if !ok {
				index++
				errors = append(errors, ValidationError{
					Index:        index,
					TableName:    tableName,
					Key:          key,
					ErrorCode:    "SYNTAX_ERROR",
					ErrorMessage: fmt.Sprintf("Entry %s|%s: expected object, got %T", tableName, key, fieldsRaw),
				})
				continue
			}

			// Convert field values to strings for CVL
			data := make(map[string]string)
			for k, v := range fieldsMap {
				switch val := v.(type) {
				case string:
					data[k] = val
				case float64:
					data[k] = fmt.Sprintf("%v", val)
				case bool:
					if val {
						data[k] = "true"
					} else {
						data[k] = "false"
					}
				case []interface{}:
					// Leaf-list: join with comma
					parts := make([]string, len(val))
					for i, item := range val {
						parts[i] = fmt.Sprintf("%v", item)
					}
					data[k] = fmt.Sprintf("%v", parts)
				default:
					data[k] = fmt.Sprintf("%v", v)
				}
			}

			editData := []cmn.CVLEditConfigData{
				{
					VType: cmn.VALIDATE_ALL,
					VOp:   cmn.OP_CREATE,
					Key:   fmt.Sprintf("%s|%s", tableName, key),
					Data:  data,
				},
			}

			cvlErr, ret := session.ValidateEditConfig(editData)
			if ret != cvl.CVL_SUCCESS {
				index++
				errors = append(errors, ValidationError{
					Index:        index,
					TableName:    cvlErr.TableName,
					Key:          fmt.Sprintf("%v", cvlErr.Keys),
					Field:        cvlErr.Field,
					ErrorCode:    errorCodeToString(cvlErr.ErrCode),
					ErrorMessage: cvlErr.Msg,
					Constraint:   cvlErr.ConstraintErrMsg,
				})
			}
		}
	}

	return errors
}

var rpc_validate_config_cb RpcCallpoint = func(body []byte, dbs [db.MaxDB]*db.DB) ([]byte, error) {
	var operand struct {
		Input struct {
			ConfigDBJson string `json:"config-db-json"`
		} `json:"sonic-config-validate:input"`
	}

	err := json.Unmarshal(body, &operand)
	if err != nil {
		glog.Errorf("validate-config: failed to parse RPC input: %v", err)
		return nil, fmt.Errorf("invalid RPC input: %v", err)
	}

	if operand.Input.ConfigDBJson == "" {
		return nil, fmt.Errorf("config-db-json is required")
	}

	// Parse the config_db JSON string
	var configDB map[string]interface{}
	err = json.Unmarshal([]byte(operand.Input.ConfigDBJson), &configDB)
	if err != nil {
		glog.Errorf("validate-config: failed to parse config_db JSON: %v", err)
		return nil, fmt.Errorf("invalid config_db JSON: %v", err)
	}

	glog.Infof("validate-config: validating %d tables", len(configDB))

	// Run validation
	validationErrors := validateConfigDB(configDB, dbs)

	// Build response
	type OutputError struct {
		Index        uint32 `json:"index"`
		TableName    string `json:"table-name"`
		Key          string `json:"key"`
		Field        string `json:"field"`
		ErrorCode    string `json:"error-code"`
		ErrorMessage string `json:"error-message"`
		Constraint   string `json:"constraint"`
	}

	var response struct {
		Output struct {
			Valid      bool          `json:"valid"`
			ErrorCount uint32        `json:"error-count"`
			Errors     []OutputError `json:"errors"`
		} `json:"sonic-config-validate:output"`
	}

	response.Output.Valid = len(validationErrors) == 0
	response.Output.ErrorCount = uint32(len(validationErrors))
	response.Output.Errors = make([]OutputError, len(validationErrors))

	for i, ve := range validationErrors {
		response.Output.Errors[i] = OutputError{
			Index:        ve.Index,
			TableName:    ve.TableName,
			Key:          ve.Key,
			Field:        ve.Field,
			ErrorCode:    ve.ErrorCode,
			ErrorMessage: ve.ErrorMessage,
			Constraint:   ve.Constraint,
		}
	}

	result, err := json.Marshal(&response)
	if err != nil {
		glog.Errorf("validate-config: failed to marshal response: %v", err)
		return nil, fmt.Errorf("failed to build response: %v", err)
	}

	glog.Infof("validate-config: validation complete, valid=%v, errors=%d",
		response.Output.Valid, response.Output.ErrorCount)

	return result, nil
}
