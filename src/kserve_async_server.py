##############################################
# KSERVE ASYNC TEMPLATE - DUAL MODE SERVER #
##############################################
"""
Template for async inference server with KServe V2 protocol support.

This template provides:
- Sync inference (KServe V2) for single predictions
- Async inference (Kafka) for batch predictions
- Large file handling with MinIO
- Job state management with Redis
- Prometheus metrics

To customize:
1. Implement your model loading logic in YourModel.load()
2. Implement prediction logic in YourModel.predict() for sync inference
3. Implement batch prediction logic in YourModel.predict_async() for async inference
4. Update request/response models to match your use case
"""

import argparse
import asyncio
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import uuid
import json
import sys
import os
import tempfile

import kserve
from fastapi import FastAPI, Form, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field
from kserve import InferRequest, InferInput, InferOutput, InferResponse
import uvicorn
import numpy as np

# Prometheus metrics
from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry, REGISTRY
from prometheus_fastapi_instrumentator import Instrumentator

# Import Kafka, MinIO, Redis clients
from kafka_config import (
    KafkaClient, MinioClient, RedisClient, Config,
    generate_object_name, parse_minio_url
)

# Import SumoController
from sumo_controller import SumoController

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================
# PROMETHEUS METRICS
# ============================================
# Metrics are defined with _get_or_create helper to avoid
# "Duplicated timeseries" errors when the module is re-imported
# (e.g. uvicorn --reload).


def _get_or_create_metric(metric_cls, name, description, labelnames=(), **kwargs):
    """Return an existing metric or create a new one, avoiding duplicates."""
    try:
        return metric_cls(name, description, labelnames=labelnames, **kwargs)
    except ValueError:
        # Already registered — retrieve it from the default registry
        return REGISTRY._names_to_collectors[name]


# Predictions
prediction_counter = _get_or_create_metric(
    Counter,
    'model_predictions',
    'Total number of predictions',
    labelnames=['mode', 'class'],
)

prediction_duration = _get_or_create_metric(
    Histogram,
    'model_prediction_duration_seconds',
    'Time spent on predictions',
    labelnames=['mode'],
)

# Jobs
job_counter = _get_or_create_metric(
    Counter,
    'model_jobs',
    'Total number of jobs',
    labelnames=['status'],
)

job_samples = _get_or_create_metric(
    Histogram,
    'model_job_samples',
    'Number of samples per job',
    buckets=[1, 10, 50, 100, 500, 1000, 5000, 10000, 50000],
)

# Storage
storage_counter = _get_or_create_metric(
    Counter,
    'model_storage_operations',
    'Storage operations',
    labelnames=['operation', 'storage'],
)

result_size_bytes = _get_or_create_metric(
    Histogram,
    'model_result_size_bytes',
    'Size of prediction results',
    buckets=[1000, 10000, 50000, 100000, 500000, 1000000, 5000000],
)

# Model
model_ready_gauge = _get_or_create_metric(
    Gauge,
    'model_ready',
    'Whether model is ready',
)


# ============================================
# REQUEST/RESPONSE MODELS
# ============================================
# TODO: Customize these models for your use case

class PredictionInput(BaseModel):
    """Input features for prediction."""
    detector_id: int = Field(
        ..., 
        ge=1, 
        description="Detector where flow was observed.",
    )

    time: int = Field(
        ...,
        ge=0,
        description="Time instant in minutes of the observation.",
    )

    flow: int = Field(
        ...,
        get=0,
        description="Traffic flow observed at detector in time interval.",
    )


class SumoParameters(BaseModel):
    """Sumo model parameters"""
    name: str = Field(..., description="Domain name for the simulation")
    simulation_datetime: Optional[str] = Field(
        datetime.now(timezone.utc).isoformat(),
        description="Datetime of the start of the simulation."
    )
    network_url: Optional[str] = Field(None, description="MinIO URL for network file (not used if use_ngsi_downloader=True)")
    detectors_url: Optional[str] = Field(None, description="MinIO URL for detectors (not used if use_dpl_downloader=True)")
    flow_interval: Optional[int] = Field(5, description="Time interval between flow measurements in minutes")
    use_ngsi_downloader: Optional[bool] = Field(False, description="Enable NGSI-LD network download")
    ngsi_domain: Optional[str] = Field(None, description="Domain filter for NGSI-LD entities")
    use_dpl_downloader: Optional[bool] = Field(False, description="Enable DPL data download")
    dpl_domain: Optional[str] = Field(None, description="Domain filter for DPL queries")
    meso_simulation: Optional[bool] = Field(False, description="If true, the simulation runs in mesoscopic mode")
    upload_entities: Optional[bool] = Field(False, description="If true, ngsi-ld entities with the result are uploaded to DPL")
    cleanup: Optional[bool] = Field(True, description="Wether to remove the files generated by the simulation")
    dpl_schema: Optional[str] = Field(None, description="Schema where the data is stored")


class BatchPredictionRequest(BaseModel):
    """Request for batch prediction job."""
    samples: Optional[List[PredictionInput]] = Field(None, description="List of samples to predict")
    # Optional: Reference to large CSV file in MinIO
    csv_file_url: Optional[str] = Field(None, description="MinIO URL for CSV file with samples")
    parameters: SumoParameters = Field(..., description="Parameters of the simulation")


class JobResponse(BaseModel):
    """Response model for job submission."""
    job_id: str
    status: str
    message: str
    created_at: str


# ============================================
# YOUR MODEL CLASS
# ============================================

class SumoModel(kserve.Model):
    """
    Your custom model with async batch prediction support.

    Supports:
    - Sync prediction via KServe V2 (single sample)
    - Async batch prediction via Kafka (multiple samples)
    - Large CSV file processing via MinIO
    """

    def __init__(self, model_name: str):
        """Initialize the model."""
        super().__init__(model_name)
        self.name = model_name
        self.ready = False

        # Initialize Kafka, MinIO, Redis clients
        self.kafka_client = KafkaClient()
        self.minio_client = MinioClient()
        self.redis_client = RedisClient()

        # Background worker task
        self.worker_task: Optional[asyncio.Task] = None

        # TODO: Add your model attributes
        self.model = None

        # Load model
        self.load()

    def load(self):
        """
        Load your model.

        TODO: Implement your model loading logic here.
        Examples:
        - Load from local file
        - Load from S3/MinIO
        - Train from scratch
        - Load from model registry
        """
        logger.info("Loading model...")

        self.model = "SUMO"

        self.ready = True
        model_ready_gauge.set(1)  # Set metric
        logger.info("Model ready")

    def get_input_types(self) -> List[Dict]:
        """
        Return input types for KServe model metadata.

        This is used by KServe SDK to automatically generate model metadata responses.
        Refer: https://kserve.github.io/website/modelserving/inference_api/#model-metadata-response-json-object

        Example:
            return [
                {"name": "feature1", "datatype": "FP32", "shape": [1]},
                {"name": "feature2", "datatype": "INT64", "shape": [1, 10]}
            ]
        """
        return [
            {"name": "detector_id", "datatype": "INT32", "shape": [1]},
            {"name": "time", "datatype": "INT32", "shape": [1]},
            {"name": "flow", "datatype": "INT32", "shape": [1]},
        ]

    def get_output_types(self) -> List[Dict]:
        """
        Return output types for KServe model metadata.

        TODO: Override this method to define your model's output schema.

        This is used by KServe SDK to automatically generate model metadata responses.
        Refer: https://kserve.github.io/website/modelserving/inference_api/#model-metadata-response-json-object

        Example:
            return [
                {"name": "prediction", "datatype": "BYTES", "shape": [1]},
                {"name": "probabilities", "datatype": "FP32", "shape": [1, 3]}
            ]
        """
        return [
            {"name": "result_url", "datatype": "BYTES", "shape": [1]}
        ]

    def predict(self, payload: InferRequest, headers: Dict[str, str] = None) -> InferResponse:
        """
        Synchronous KServe V2 prediction for single sample.

        TODO: Implement your prediction logic.

        Input format (KServe V2):
        {
            "inputs": [
                {"name": "feature1", "data": [value1]},
                {"name": "feature2", "data": [value2]},
                ...
            ]
        }
        """
        if not self.ready:
            raise RuntimeError("Model not ready")

        logger.info("Sync KServe V2 prediction called")

        import time
        start_time = time.time()

        emission_url, edgedata_url = self.run_sumo(payload)

        duration = time.time() - start_time

        result = {
            "emission": emission_url,
            "edgedata": edgedata_url,
        }

        # Record metrics
        prediction_counter.labels(mode='sync', **{'class': str(SumoController)}).inc()
        prediction_duration.labels(mode='sync').observe(duration)

        logger.info(f"Prediction: {SumoController} (confidence: {0:.3f})")

        # Convert to KServe V2 response
        outputs = [
            InferOutput(
                name="prediction",
                shape=[1],
                datatype="BYTES",
                data=[json.dumps(result)]
            )
        ]

        return InferResponse(
            response_id=str(uuid.uuid4()),
            model_name=self.name,
            infer_outputs=outputs
        )
    
    def run_sumo(self, payload: InferRequest):
        """Extracts request parameters and summons SUMO model."""
        # Extract parameters from request in OIPv2
        name = self._get_parameter("name", payload)
        simulation_datetime = self._get_parameter("simulation_datetime", payload) or datetime.now(timezone.utc).isoformat()
        flow_interval_minutes = self._get_parameter("flow_interval_minutes", payload) or 5
        meso_simulation = self._get_parameter("meso_simulation", payload) or False
        upload_entities = self._get_parameter("upload_entities", payload) or False
        cleanup = self._get_parameter("cleanup", payload)
        if cleanup is None:
            cleanup = True

        # Check for NGSI-LD and DPL modes
        use_ngsi_downloader = self._get_parameter("use_ngsi_downloader", payload) or False
        ngsi_domain = self._get_parameter("ngsi_domain", payload)
        use_dpl_downloader = self._get_parameter("use_dpl_downloader", payload) or False
        dpl_domain = self._get_parameter("dpl_domain", payload)
        dpl_schema = self._get_parameter("dpl_schema", payload)

        # Get network and detector URLs (only required if not using NGSI-LD/DPL)
        network_url = self._get_parameter("network_url", payload) if not use_ngsi_downloader else ""
        detectors_url = self._get_parameter("detectors_url", payload) if not use_dpl_downloader else None

        # Extract flow data from request (only if not using DPL)
        flow_data = None
        if not use_dpl_downloader and payload.inputs:
            flow_data = self._get_input_data(payload)

        # Validate required parameters
        if name is None:
            raise HTTPException(
                status_code=422,
                detail="Missing required parameter: 'name'"
            )

        if not use_ngsi_downloader and network_url is None:
            raise HTTPException(
                status_code=422,
                detail="Missing 'network_url' parameter (required when use_ngsi_downloader=False)"
            )

        if not use_dpl_downloader and (detectors_url is None or flow_data is None):
            raise HTTPException(
                status_code=422,
                detail="Missing 'detectors_url' or flow data (required when use_dpl_downloader=False)"
            )

        # Create controller and run simulation
        controller = SumoController(
            name=name,
            network_url=network_url,
            detectors_url=detectors_url,
            flow_data=flow_data,
            flow_interval_minutes=flow_interval_minutes,
            simulation_datetime=simulation_datetime,
            use_ngsi_downloader=use_ngsi_downloader,
            ngsi_domain=ngsi_domain,
            use_dpl_downloader=use_dpl_downloader,
            dpl_domain=dpl_domain,
            dpl_schema=dpl_schema,
            meso_simulation=meso_simulation,
            upload_entities=upload_entities,
            cleanup=cleanup,
        )
        emission_url, edgedata_url = controller.run()
        return emission_url, edgedata_url

    @staticmethod
    def _get_parameter(parameter: str, payload: kserve.InferRequest) -> dict | None:
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
        except KeyError:
            logging.warning(f"'{parameter}' field not found in request.")
            return

    @staticmethod
    def _get_input_data(payload: kserve.InferRequest) -> np.ndarray:
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

    def _get_async_samples(self, samples: List[Dict]) -> np.ndarray:
        """Extract and format input samples from input request.

        Args:
            samples (List[Dict]): _description_

        Returns:
            np.ndarray: _description_
        """
        samples = np.array([
            [sample["detector_id"], sample["time"], sample["flow"]]
            for sample in samples
        ])
        return samples
    
    def _parse_samples_from_minio(self, csv_content):
        """Parses the input samples downloaded as csv from MinIO.

        Args:
            csv_content (_type_): _description_
        """
        csv_content = csv_content.strip()  # Remove last blank line
        return np.ndarray(
            [line.split(',') for line in csv_content.split('\n')]
        )

    async def predict_async(self, job_id: str, inputs: Dict[str, Any]):
        """
        Asynchronous batch prediction.

        Processes multiple samples and can handle large CSV files from MinIO.
        Supports NGSI-LD network download and DPL data download.
        """
        if not self.ready:
            await self._update_job_status(job_id, {
                "status": "failed",
                "error": "Model not ready",
                "failed_at": datetime.now(timezone.utc).isoformat()
            })
            return

        try:
            # Update status to running
            await self._update_job_status(job_id, {
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "progress": 0
            })
            logger.info(f"Job {job_id} started - Batch prediction")

            # Get SUMO parameters
            if not "parameters" in inputs:
                raise ValueError("No parameters provided for simulation")
            sumo_params = inputs["parameters"]
            domain_name = sumo_params["name"]
            simulation_datetime = sumo_params.get("simulation_datetime")
            flow_interval_minutes = sumo_params.get("flow_interval", 5)

            # Check for NGSI-LD and DPL modes
            use_ngsi_downloader = sumo_params.get("use_ngsi_downloader", False)
            ngsi_domain = sumo_params.get("ngsi_domain")
            use_dpl_downloader = sumo_params.get("use_dpl_downloader", False)
            dpl_domain = sumo_params.get("dpl_domain")
            cleanup = sumo_params.get("cleanup", False)
            dpl_schema = sumo_params.get("dpl_schema", None)
            meso_simulation = sumo_params.get("meso_simulation", False)
            upload_entities = sumo_params.get("upload_entities", False)

            # Get samples (only if not using DPL)
            samples = None
            if not use_dpl_downloader:
                # Option 1: Samples provided directly in request
                if 'samples' in inputs and inputs['samples']:
                    logger.info(f"Processing {len(inputs['samples'])} samples from request")
                    samples = self._get_async_samples(inputs['samples'])
                    await self._update_job_status(job_id, {"progress": 10})

                # Option 2: CSV file in MinIO
                elif inputs.get('csv_file_url'):
                    logger.info(f"Downloading CSV from MinIO for job {job_id}")
                    bucket, object_name = parse_minio_url(inputs['csv_file_url'])
                    csv_bytes = self.minio_client.download_bytes(object_name)
                    csv_content = csv_bytes.decode('utf-8')
                    samples = self._parse_samples_from_minio(csv_content)
                    logger.info(f"Loaded {len(samples)} samples from CSV")
                    await self._update_job_status(job_id, {"progress": 20})
                else:
                    raise ValueError("No samples provided and use_dpl_downloader is False")
            else:
                logger.info(f"Using DPL downloader - samples will be downloaded from DPL")
                await self._update_job_status(job_id, {"progress": 15})

            # Get network and detector URLs (only if not using NGSI-LD/DPL)
            network_url = sumo_params.get("network_url") if not use_ngsi_downloader else ""
            detectors_url = sumo_params.get("detectors_url") if not use_dpl_downloader else None

            await self._update_job_status(job_id, {"progress": 30})

            # Run SUMO simulation
            logger.info(f"Starting SUMO simulation for domain '{domain_name}'")
            logger.info(f"  - NGSI-LD mode: {use_ngsi_downloader}")
            logger.info(f"  - DPL mode: {use_dpl_downloader}")

            controller = SumoController(
                name=domain_name,
                network_url=network_url,
                detectors_url=detectors_url,
                flow_data=samples,
                flow_interval_minutes=flow_interval_minutes,
                simulation_datetime=simulation_datetime,
                use_ngsi_downloader=use_ngsi_downloader,
                ngsi_domain=ngsi_domain,
                use_dpl_downloader=use_dpl_downloader,
                dpl_domain=dpl_domain,
                dpl_schema=dpl_schema,
                cleanup=cleanup,
                meso_simulation=meso_simulation,
                upload_entities=upload_entities,
            )

            await self._update_job_status(job_id, {"progress": 40})

            # Run simulation in thread pool to avoid blocking
            await asyncio.to_thread(controller.run)

            results = [controller.emission_url, controller.edgedata_url]

            await self._update_job_status(job_id, {"progress": 70})

            # Create result summary
            result = {
                "total_samples": 0,
                "predictions": results,
                "summary": {
                    "total": 0
                    # Add your summary statistics here
                },
                "processed_at": datetime.now(timezone.utc).isoformat()
            }

            await self._update_job_status(job_id, {"progress": 90})

            # If result is large, store in MinIO
            # This part is handled in the SumoController class

            # Record metrics for completed job
            job_counter.labels(status='completed').inc()
            job_samples.observe(1)

            # Update job with result
            await self._update_job_status(job_id, {
                "status": "completed",
                "result": result,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "progress": 100
            })
            logger.info(f"Job {job_id} completed")

        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            job_counter.labels(status='failed').inc()
            await self._update_job_status(job_id, {
                "status": "failed",
                "error": str(e),
                "failed_at": datetime.now(timezone.utc).isoformat()
            })

    async def _update_job_status(self, job_id: str, status_update: Dict[str, Any]):
        """Update job status in Redis and publish to Kafka."""
        current_status = self.redis_client.get_job_status(job_id) or {}
        current_status.update(status_update)
        current_status['job_id'] = job_id
        self.redis_client.set_job_status(job_id, current_status)
        await self.kafka_client.publish_job_status(job_id, current_status)

    async def submit_job(self, **kwargs) -> Dict[str, Any]:
        """Submit a new batch prediction job to Kafka queue."""
        job_id = str(uuid.uuid4())

        job = {
            "job_id": job_id,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "progress": 0,
            "inputs": kwargs
        }

        self.redis_client.set_job_status(job_id, job)
        await self.kafka_client.publish_job_request(job_id, kwargs)

        # Record metrics
        job_counter.labels(status='pending').inc()
        storage_counter.labels(operation='write', storage='redis').inc()
        storage_counter.labels(operation='publish', storage='kafka').inc()

        logger.info(f"Batch job {job_id} submitted to Kafka queue")

        return {
            "job_id": job_id,
            "status": "pending",
            "message": "Batch prediction job submitted to queue",
            "created_at": job["created_at"]
        }

    async def start_worker(self):
        """Start background Kafka consumer worker."""
        logger.info("Starting Kafka consumer worker for batch predictions...")

        try:
            async for job_message in self.kafka_client.consume_job_requests():
                job_id = job_message['job_id']
                job_data = job_message['data']

                logger.info(f"Processing batch job {job_id} from Kafka queue")
                await self.predict_async(job_id, job_data)

        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            await asyncio.sleep(5)
            await self.start_worker()

    async def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Get job status from Redis."""
        job = self.redis_client.get_job_status(job_id)
        if job is None:
            return {"error": f"Job {job_id} not found"}

        return {
            "job_id": job_id,
            "status": job.get("status", "unknown"),
            "progress": job.get("progress", 0),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
            "failed_at": job.get("failed_at"),
            "error": job.get("error")
        }

    async def get_job_result(self, job_id: str) -> Dict[str, Any]:
        """Get job result from Redis/MinIO."""
        job = self.redis_client.get_job_status(job_id)
        if job is None:
            return {"error": f"Job {job_id} not found"}

        if job["status"] == "completed":
            result = job.get("result", {})

            return {
                "job_id": job_id,
                "status": "completed",
                "result": result,
                "completed_at": job.get("completed_at")
            }
        elif job["status"] == "running":
            return {
                "error": "Job still running",
                "status": "running",
                "progress": job.get("progress", 0)
            }
        elif job["status"] == "failed":
            return {
                "error": f"Job failed: {job.get('error', 'Unknown error')}",
                "status": "failed"
            }
        else:
            return {
                "error": "Job not completed yet",
                "status": job.get("status", "unknown")
            }

    async def list_jobs(self, status: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        """List all jobs from Redis."""
        jobs = self.redis_client.list_jobs(limit=limit)
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        jobs = jobs[:limit]
        return {"jobs": jobs, "total": len(jobs)}

    async def cancel_job(self, job_id: str) -> Dict[str, Any]:
        """Cancel a running job."""
        job = self.redis_client.get_job_status(job_id)
        if job is None:
            return {"error": f"Job {job_id} not found"}
        if job.get("status") in ["completed", "failed"]:
            return {"error": f"Job {job_id} already {job['status']}"}

        job["cancelled"] = True
        self.redis_client.set_job_status(job_id, job)
        await self.kafka_client.publish_job_status(job_id, {"cancelled": True})

        return {"message": f"Job {job_id} cancellation requested", "job_id": job_id}


# ============================================
# FASTAPI APPLICATION
# ============================================

app = FastAPI(
    title="Async Inference API",
    description="Dual-mode inference server with sync and async batch prediction",
    version="1.0.0"
)

# Instrument FastAPI with Prometheus metrics
Instrumentator().instrument(app).expose(app)

_model: Optional[SumoModel] = None


def get_model() -> SumoModel:
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not ready")
    return _model


@app.on_event("startup")
async def startup_event():
    global _model
    model_name = os.environ.get('MODEL_NAME', 'your-model')

    logger.info("="*60)
    logger.info(f"STARTUP - Async Inference Server")
    logger.info(f"Model: {model_name}")
    logger.info(f"Kafka: {Config.KAFKA_BOOTSTRAP_SERVERS}")
    logger.info(f"MinIO: {Config.MINIO_ENDPOINT}")
    logger.info(f"Redis: {Config.REDIS_HOST}:{Config.REDIS_PORT}")
    logger.info("="*60)

    try:
        _model = SumoModel(model_name)
        _model.worker_task = asyncio.create_task(_model.start_worker())
        logger.info("Model ready for predictions")
    except Exception as e:
        logger.error(f"Failed to load model: {e}", exc_info=True)
        raise


@app.on_event("shutdown")
async def shutdown_event():
    global _model
    if _model:
        if _model.worker_task:
            _model.worker_task.cancel()
            try:
                await _model.worker_task
            except asyncio.CancelledError:
                pass
        await _model.kafka_client.close()
        _model.redis_client.close()
        logger.info("Shutdown complete")


# Health endpoints
@app.get("/")
async def root():
    return {
        "service": "Async Inference API",
        "version": "1.0.0",
        "modes": ["sync (KServe V2)", "async (Kafka batch)"]
    }


@app.get("/health")
async def health():
    try:
        model = get_model()
        return {"status": "healthy", "model_ready": model.ready}
    except HTTPException:
        return {"status": "healthy", "model_ready": False}


# KServe V2 endpoints
@app.get("/v2")
async def get_server_metadata():
    """Get server metadata (KServe V2)."""
    return {
        "name": "async-inference-server",
        "version": "1.0.0",
        "extensions": ["async-batch", "minio-storage"]  # TODO: Update with your extensions
    }


@app.get("/v2/health/live")
async def server_live():
    """Server liveness endpoint (KServe V2)."""
    return {"live": True}


@app.get("/v2/health/ready")
async def server_ready():
    """Server readiness endpoint - all models ready (KServe V2)."""
    try:
        model = get_model()
        return {"ready": model.ready}
    except HTTPException:
        return {"ready": False}


@app.get("/v2/models/{model_name}/ready")
async def model_ready(model_name: str):
    """Model readiness endpoint (KServe V2)."""
    try:
        model = get_model()
        if model.name != model_name:
            raise HTTPException(status_code=404, detail=f"Model {model_name} not found")
        return {"ready": model.ready}
    except HTTPException:
        raise


@app.get("/v2/models/{model_name}")
async def get_model_metadata(model_name: str):
    """Get model metadata (KServe V2)."""
    model = get_model()
    if model.name != model_name:
        raise HTTPException(status_code=404, detail=f"Model {model_name} not found")

    return {
        "name": model.name,
        "versions": ["v1"],
        "platform": "custom",  # TODO: Update platform name (e.g., "pytorch", "tensorflow", "sklearn")
        "inputs": model.get_input_types(),
        "outputs": model.get_output_types()
    }


@app.post("/v2/models/{model_name}/infer")
async def sync_infer(model_name: str, request: dict):
    """Synchronous single-sample prediction (KServe V2).

    Accepts the same parameters as POST /v1/jobs via a 'parameters' field.
    When 'use_dpl_downloader' is True inside parameters, the 'inputs' field
    is ignored and flow data will be fetched by the DPLDownloader.
    """
    model = get_model()
    if model.name != model_name:
        raise HTTPException(status_code=404, detail=f"Model {model_name} not found")

    try:
        parameters = request.get("parameters", {})
        use_dpl_downloader = parameters.get("use_dpl_downloader", False)

        infer_inputs = []
        if not use_dpl_downloader:
            for inp in request.get("inputs", []):
                infer_inputs.append(
                    InferInput(
                        name=inp["name"],
                        shape=inp["shape"],
                        datatype=inp["datatype"],
                        data=inp["data"]
                    )
                )

        infer_request = InferRequest(
            model_name=model_name,
            infer_inputs=infer_inputs,
            parameters=parameters,
        )
        # Run predict in thread pool to avoid blocking event loop during long-running predictions
        response = await asyncio.to_thread(model.predict, infer_request)

        return {
            "model_name": response.model_name,
            "id": getattr(response, 'id', getattr(response, 'response_id', 'unknown')),
            "outputs": [
                {"name": out.name, "shape": out.shape, "datatype": out.datatype, "data": out.data}
                for out in response.outputs
            ]
        }
    except Exception as e:
        logger.error(f"Sync inference failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# File upload endpoint
@app.post("/v1/files/upload")
async def upload_file(
    file: UploadFile = File(...), 
    file_id: Optional[str] = Form(None),
    filename: Optional[str] = Form(None),
):
    """Upload CSV file with samples to MinIO.

    Args:
        file: File to upload.
        file_id: ID of the file. Will be attached as a prefix to the object name.
        filename: Name (without extension) to use for the stored object.
                  Defaults to the original file's stem if not provided.
    """
    model = get_model()

    if not file_id:
        file_id = str(uuid.uuid4())
    extension = os.path.splitext(file.filename)[1] if file.filename else '.csv'
    stem = filename or (os.path.splitext(file.filename)[0] if file.filename else 'input')
    object_name = generate_object_name(file_id, stem, extension)

    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        content = await file.read()
        tmp_file.write(content)
        tmp_file.flush()

        url = model.minio_client.upload_file(
            object_name,
            tmp_file.name,
            file.content_type or 'text/csv'
        )

    os.unlink(tmp_file.name)

    # Record metrics
    storage_counter.labels(operation='upload', storage='minio').inc()
    result_size_bytes.observe(len(content))

    return {
        "file_id": file_id,
        "url": url,
        "filename": file.filename,
        "size": len(content)
    }


@app.get("/v1/files/download")
async def download_file_from_minio(url: str = Query(..., description="MinIO URL (minio://bucket/path/to/file)")):
    """
    Download file from MinIO.

    Args:
        url: MinIO URL in format minio://bucket/object-name

    Returns:
        File content as streaming response
    """
    from fastapi.responses import StreamingResponse
    import io

    model = get_model()

    try:
        # Parse MinIO URL
        bucket, object_name = parse_minio_url(url)
        logger.info(f"OBJECT NAME: {object_name}")

        # Download file bytes from MinIO
        file_bytes = model.minio_client.download_bytes(object_name)

        # Record metrics
        storage_counter.labels(operation='download', storage='minio').inc()
        result_size_bytes.observe(len(file_bytes))

        # Determine content type based on extension
        content_type = "application/octet-stream"
        if object_name.endswith('.csv'):
            content_type = "text/csv"
        elif object_name.endswith('.json'):
            content_type = "application/json"
        elif object_name.endswith('.txt'):
            content_type = "text/plain"
        elif object_name.endswith('.pdf'):
            content_type = "application/pdf"
        elif object_name.endswith('.png'):
            content_type = "image/png"
        elif object_name.endswith('.jpg') or object_name.endswith('.jpeg'):
            content_type = "image/jpeg"

        # Get filename from object_name
        filename = object_name.split('/')[-1]

        # Return streaming response
        return StreamingResponse(
            io.BytesIO(file_bytes),
            media_type=content_type,
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Length": str(len(file_bytes))
            }
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid MinIO URL: {str(e)}")
    except Exception as e:
        logger.error(f"Failed to download file from MinIO: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}")


@app.get("/v1/files")
async def list_files() -> List[str]:
    """List all files in the configured bucket for the app."""
    model = get_model()
    try:
        bucket = model.minio_client.bucket
        bucket_files = model.minio_client.client.list_objects(bucket, recursive=True)
        return [f.object_name for f in bucket_files]
    except Exception as e:
        logger.error(f"Failed to list objects from bucket: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error while listing files: {str(e)}")


# Async batch endpoints
@app.post("/v1/jobs", response_model=JobResponse)
async def submit_job(submission: BatchPredictionRequest):
    """Submit batch prediction job."""
    model = get_model()
    inputs = submission.dict()
    result = await model.submit_job(**inputs)
    return result


@app.get("/v1/jobs/{job_id}")
async def get_job_status_endpoint(job_id: str):
    model = get_model()
    result = await model.get_job_status(job_id)
    if "error" in result and result["error"] is not None:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/v1/jobs/{job_id}/result")
async def get_job_result_endpoint(job_id: str):
    model = get_model()
    result = await model.get_job_result(job_id)
    if "error" in result and result["error"] is not None:
        if result.get("status") == "running":
            raise HTTPException(status_code=202, detail=f"Job still running, progress: {result.get('progress', 0)}%")
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/v1/jobs")
async def list_jobs_endpoint(status: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=1000)):
    model = get_model()
    result = await model.list_jobs(status=status, limit=limit)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.delete("/v1/jobs/{job_id}")
async def cancel_job_endpoint(job_id: str):
    model = get_model()
    result = await model.cancel_job(job_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Async Inference Server")
    parser.add_argument("--model_name", type=str, default="your-model", help="Model name")
    parser.add_argument("--http_port", type=int, default=8080, help="HTTP port")
    parser.add_argument("--reload", type=bool, default=False, help="Reload with changes")
    args, _ = parser.parse_known_args()

    os.environ['MODEL_NAME'] = args.model_name

    logger.info(f"Starting server on port {args.http_port}")
    if args.reload:
        # Add src directory to Python path for module resolution during reload
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        uvicorn.run("kserve_async_server:app", host="0.0.0.0", port=args.http_port, log_level="info", reload=True, reload_dirs=["src"])
    else:
        uvicorn.run(app, host="0.0.0.0", port=args.http_port, log_level="info")
