# Copyright 2019 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
import json
import logging

from error_code.error_status import WorkOrderStatus
from error_code.enclave_error import EnclaveError
from avalon_sdk.connector.direct.jrpc.jrpc_util import JsonRpcErrorCode
from avalon_sdk.work_order.work_order_request_validator \
    import WorkOrderRequestValidator
from utility.hex_utils import is_valid_hex_str

from jsonrpc.exceptions import JSONRPCDispatchException

logger = logging.getLogger(__name__)

# XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
# XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX


class TCSWorkOrderHandler:
    """
    TCSWorkOrderHandler processes Worker Order Direct API requests. It puts a
    new work order requests into the KV storage or it reads appropriate work
    order information from the KV storage when processing “getter” and
    enumeration requests. Actual work order processing is done by the
    Intel SGX Enclave Manager within the enclaves its manages.
    All raised exceptions will be caught and handled by any
    jsonrpc.dispatcher.Dispatcher delegating work to this handler. In our case,
    the exact dispatcher will be the one configured by the TCSListener in the
    ./tcs_listener.py
    """
# ------------------------------------------------------------------------------------------------

    def __init__(self, kv_helper, max_wo_count):
        """
        Function to perform init activity
        Parameters:
            - kv_helper is a object of lmdb database
        """

        self.kv_helper = kv_helper
        self.workorder_count = 0
        self.max_workorder_count = max_wo_count
        self.workorder_list = []
        self.__work_order_handler_on_boot()

# ---------------------------------------------------------------------------------------------
    def __work_order_handler_on_boot(self):
        """
        Function to perform on-boot process of work order handler
        """

        work_orders = self.kv_helper.lookup("wo-timestamps")
        for wo_id in work_orders:

            if(self.kv_helper.get("wo-scheduled", wo_id) is None and
                    self.kv_helper.get("wo-processing", wo_id) is None and
                    self.kv_helper.get("wo-processed", wo_id) is None):

                if(self.kv_helper.get("wo-requests", wo_id) is not None):
                    self.kv_helper.remove("wo-requests", wo_id)

                if(self.kv_helper.get("wo-responses", wo_id) is not None):
                    self.kv_helper.remove("wo-responses", wo_id)

                if(self.kv_helper.get("wo-receipts", wo_id) is not None):
                    self.kv_helper.remove("wo-receipts", wo_id)

                # TODO: uncomment after fixing lmbd error
                self.kv_helper.remove("wo-timestamps", wo_id)

            else:
                # Add to the internal FIFO
                self.workorder_list.append(wo_id)
                self.workorder_count += 1

    def _is_worker_exists(self, worker_id):
        """
        Function to check if worker is exists or not
        Returns
            True if exists or False if not
        """
        logger.info("worker id to check in lmdb {}".format(worker_id))
        if self.kv_helper.get("workers", worker_id):
            return True
        else:
            return False

# ---------------------------------------------------------------------------------------------
    def WorkOrderGetResult(self, **params):
        """
        Function to process work order get result.
        This API corresponds to Trusted Compute EEA API 6.1.4
        Work Order Pull Request Payload
        Parameters:
            - params is variable-length argument list containing work request
              as defined in EEA spec 6.1.4
        Returns jrpc response as defined in EEA spec 6.1.2
        """
        if "workOrderId" in params:
            wo_id = params["workOrderId"]
            # Work order status payload should have
            # data field with work order id as defined in EEA spec section 6.
            data = {
                "workOrderId": wo_id
            }
            if not is_valid_hex_str(wo_id):
                logging.error("Invalid work order Id")
                raise JSONRPCDispatchException(
                    JsonRpcErrorCode.INVALID_PARAMETER,
                    "Invalid work order Id",
                    data
                )

            # Work order is processed if it is in wo-response table
            value = self.kv_helper.get("wo-responses", wo_id)
            if value:
                response = json.loads(value)
                if 'result' in response:
                    return response['result']

                # response without a result should have an error
                err_code = response["error"]["code"]
                err_msg = response["error"]["message"]

                if err_code == EnclaveError.ENCLAVE_ERR_VALUE:
                    err_code = \
                        WorkOrderStatus.INVALID_PARAMETER_FORMAT_OR_VALUE
                elif err_code == EnclaveError.ENCLAVE_ERR_UNKNOWN:
                    err_code = WorkOrderStatus.UNKNOWN_ERROR
                elif err_code == EnclaveError.ENCLAVE_ERR_INVALID_WORKLOAD:
                    err_code = WorkOrderStatus.INVALID_WORKLOAD
                else:
                    err_code = WorkOrderStatus.FAILED
                raise JSONRPCDispatchException(
                    err_code,
                    err_msg,
                    data
                )

            if(self.kv_helper.get("wo-timestamps", wo_id) is not None):
                # work order is yet to be processed
                raise JSONRPCDispatchException(
                    WorkOrderStatus.PENDING,
                    "Work order result is yet to be updated",
                    data)

            # work order not in 'wo-timestamps' table
            raise JSONRPCDispatchException(
                WorkOrderStatus.INVALID_PARAMETER_FORMAT_OR_VALUE,
                "Work order Id not found in the database. " +
                "Hence invalid parameter",
                data)
        else:
            raise JSONRPCDispatchException(
                WorkOrderStatus.INVALID_PARAMETER_FORMAT_OR_VALUE,
                "Missing work order id"
            )

# ---------------------------------------------------------------------------------------------
    def WorkOrderSubmit(self, **params):
        """
        Function to process work order request
        Parameters:
            - params is variable-length argument list containing work request
              as defined in EEA spec 6.1.1
        Returns jrpc response as defined in EEA spec 6.1.3
        """

        wo_id = params["workOrderId"]
        input_json_str = params["raw"]
        input_value_json = json.loads(input_json_str)
        # Work order status payload should have
        # data filed with work order id as defined in EEA spec section 6.
        data = {
            "workOrderId": wo_id
        }
        req_validator = WorkOrderRequestValidator()
        valid, err_msg = req_validator.validate_parameters(
            input_value_json["params"])
        if not valid:
            raise JSONRPCDispatchException(
                JsonRpcErrorCode.INVALID_PARAMETER,
                err_msg,
                data
            )

        worker_id = input_value_json["params"]["workerId"]

        if "inData" in input_value_json["params"]:
            valid, err_msg = req_validator.validate_data_format(
                input_value_json["params"]["inData"])
            if not valid:
                raise JSONRPCDispatchException(
                    JsonRpcErrorCode.INVALID_PARAMETER,
                    err_msg,
                    data)
        else:
            raise JSONRPCDispatchException(
                JsonRpcErrorCode.INVALID_PARAMETER,
                "Missing inData parameter",
                data
            )

        if "outData" in input_value_json["params"]:
            valid, err_msg = req_validator.validate_data_format(
                input_value_json["params"]["outData"])
            if not valid:
                raise JSONRPCDispatchException(
                    JsonRpcErrorCode.INVALID_PARAMETER,
                    err_msg,
                    data)
        # Check if workerId is exists in avalon
        if not self._is_worker_exists(worker_id):
            raise JSONRPCDispatchException(
                JsonRpcErrorCode.INVALID_PARAMETER,
                "worker {} doesn't exists".format(worker_id),
                data
            )

        if((self.workorder_count + 1) > self.max_workorder_count):

            # if max count reached clear a processed entry
            work_orders = self.kv_helper.lookup("wo-timestamps")
            for id in work_orders:

                # If work order is processed then remove from table
                if(self.kv_helper.get("wo-processed", id) is not None):
                    self.kv_helper.remove("wo-processed", id)
                    self.kv_helper.remove("wo-requests", id)
                    self.kv_helper.remove("wo-responses", id)
                    self.kv_helper.remove("wo-receipts", id)
                    self.kv_helper.remove("wo-timestamps", id)

                    self.workorder_list.remove(id)
                    self.workorder_count -= 1
                    break

            # If no work order is processed then return busy
            if((self.workorder_count + 1) > self.max_workorder_count):
                raise JSONRPCDispatchException(
                    WorkOrderStatus.BUSY,
                    "Work order handler is busy updating the result",
                    data)

        if(self.kv_helper.get("wo-timestamps", wo_id) is None):

            # Create a new work order entry.
            # Don't change the order of table updation.
            # The order is important for clean up if the TCS is restarted in
            # the middle.
            # Add entry to wo-worker-pending which holds all the work order id
            # separated by comma(csv) to be processed by corresponding worker.
            # i.e. - <worker_id> -> <wo_id>,<wo_id>,<wo_id>...
            epoch_time = str(time.time())

            # Update the tables
            self.kv_helper.set("wo-timestamps", wo_id, epoch_time)
            self.kv_helper.set("wo-requests", wo_id, input_json_str)
            self.kv_helper.csv_append("wo-worker-pending", worker_id, wo_id)
            # Add to the internal FIFO
            self.workorder_list.append(wo_id)
            self.workorder_count += 1
            raise JSONRPCDispatchException(
                WorkOrderStatus.PENDING,
                "Work order is computing. Please query for WorkOrderGetResult \
                to view the result",
                data)

        # Workorder id already exists
        raise JSONRPCDispatchException(
            WorkOrderStatus.INVALID_PARAMETER_FORMAT_OR_VALUE,
            "Work order id already exists in the database. "
            + "Hence invalid parameter",
            data)

# ---------------------------------------------------------------------------------------------
