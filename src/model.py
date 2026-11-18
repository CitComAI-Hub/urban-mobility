import uuid
import kserve
import numpy as np

from config import settings
from logger import appLogging as logging
from sumo_controller import SumoController


class SumoModel(kserve.Model):
    def __init__(self, name, predictor_config = None, return_response_headers = False):
        super().__init__(name, predictor_config, return_response_headers)
        self.load()
        self.result_url = "Simulation failed."

    def load(self):
        self.ready = True

    def predict(self, payload: kserve.InferRequest, headers = None, response_headers = None):
        """Extracts and arranges input data from payload, and executes SUMO from the data.

        Args:
            payload (_type_): Request for the simulation. Must be provided in OpenInferenceProtocol (V2).
            headers (_type_, optional): _description_. Defaults to None.
            response_headers (_type_, optional): _description_. Defaults to None.
        """
        # Extract name from request in OIPv2
        name = self.__get_parameter("name", payload)

        # Extract id or create a random one
        request_id = payload.id if payload.id is not None else str(uuid.uuid4())

        # Extract network download url
        network_url = self.__get_parameter("network_url", payload)

        # Extract detector data from request
        detectors_url = self.__get_parameter("detectors_url", payload)

        # Extract data from request
        flow_data = self.__get_input_data(payload)
        flow_interval_minutes = self.__get_parameter("flow_interval_minutes", payload)

        # If all fields are found, define the controller and start the simulation
        if all(x is not None for x in (name, network_url, detectors_url, flow_interval_minutes)):
            controller = SumoController(name, network_url, detectors_url, flow_data, flow_interval_minutes)
            controller.run()
            self.result_url = controller.result_rul

        outputs = {  # "outputs" field is added just because it is mandatory in Open Inference Protocol
            "model_name": settings.MODEL_NAME,
            "model_version": settings.MODEL_VERSION,
            "id": request_id,
            "parameters": {
                "result_url": self.result_url
            },
            "outputs": [{"name": "ignore", "shape": [1], "datatype": "INT4", "data": [0]}]
        }
        return outputs

    @staticmethod
    def __get_input_data(payload: kserve.InferRequest) -> np.ndarray:
        """Returns the first input data batch from an InferRequest object.
        Only the first input will be considered, all others are discard.

        Args:
            payload (kserve.InferRequest): Inference request.
        Returns:
            np.ndarray: "data" field of the request.
        """
        input = payload.inputs[0]
        shape = input.shape
        return np.array(input.data).reshape(shape)

    @staticmethod
    def __get_parameter(parameter: str, payload: kserve.InferRequest) -> dict | None:
        """Attempts to get "parameter" from the request parameters field. If "parameter" 
        is not present in the request, returns "None" to abort the simulation.

        Args:
            payload (kserve.InferRequest): Inference request.

        Returns:
            dict | None: Parameter.
        """
        parameters = payload.parameters
        try:
            return parameters[parameter]
        except KeyError as error:
            logging.error(f"'{parameter}' field not found in request. Aborting simulation.")
            return

