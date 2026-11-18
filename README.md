# KServe Interface for SUMO

## About SUMO

**SUMO** (Simulation of Urban MObility) is an open-source, highly portable, microscopic and continuous multi-modal traffic simulation suite. It is designed to handle large networks and allows modeling of intermodal traffic systems including road vehicles, public transport, and pedestrians.

### Key Features

- **Microscopic simulation**: Models individual vehicles with their own routes and behaviors
- **Multi-modal support**: Simulates cars, trucks, buses, trains, bicycles, and pedestrians
- **Network modeling**: Supports complex road networks with traffic lights, junctions, and lane changes
- **Traffic demand modeling**: Flexible input formats for traffic flows and routes
- **Open source**: Developed by the German Aerospace Center (DLR) and freely available
- **Extensible**: Python API (TraCI) for runtime interaction and control

SUMO is widely used in traffic research, urban planning, and intelligent transportation systems for evaluating traffic management strategies, testing autonomous vehicle algorithms, and analyzing environmental impacts of traffic.

**Official website**: https://eclipse.dev/sumo/

## Overview

This project integrates SUMO within a KServe interface, providing both synchronous and asynchronous inference modes via the [Open Inference Protocol](https://kserve.github.io/website/docs/concepts/architecture/data-plane/v2-protocol) (OIP).

## Clone the repo
```
git clone --recurse-submodules <repo-url> 
```

## Services required

This service requires that the following utilities are accessible from the container of the application. For deployments with kubeflow, the relative endpoints are listed in the YAML file: 

- MinIO service: MINIO_ENDPOINT
- Kafka service: KAFKA_BOOTSTRAP_SERVERS
- Redis service: REDIS_HOST


## Deployment Modes

The service supports two operational modes:

1. **Synchronous Mode**: Standard KServe V2 protocol for single predictions
2. **Asynchronous Mode**: Kafka-based batch predictions with job management

## Synchronous Mode (KServe V2)

### Endpoint
```
POST /v2/models/{model_name}/infer
```

### Request Schema
The OIP request must follow this schema:

```json
{
  "id": "string",
  "parameters": {
    "name": "string",
    "network_url": "string",
    "detectors_url": "string",
    "flow_interval_minutes": "integer",
    "simulation_datetime": "string",
    "result_url": "string"
  },
  "inputs": [
    {
      "name": "flow_data",
      "shape": [N, 3],
      "datatype": "INT32",
      "data": [...]
    }
  ]
}
```

### Parameters
- **name** (required): Domain name for the simulation
- **network_url** (required): MinIO URL to the SUMO network XML file (e.g., `minio://bucket/domain/net.net.xml`)
- **detectors_url** (required): MinIO URL to the detectors JSON file (e.g., `minio://bucket/domain/detectors.json`)
- **flow_interval_minutes** (required): Time interval in minutes between each flow observation
- **simulation_datetime** (optional): ISO format datetime for the simulation start (defaults to current UTC time)
- **result_url** (optional): MinIO URL where results will be uploaded (defaults to same directory as network_url with datetime as filename)

### Input Data
- **shape**: [N_detectors × N_timestamps, 3]
- **datatype**: INT32
- **data**: Flattened array where each row contains [detector_id, time_minutes, flow]

### Example Request

```json
{
  "id": "test_run",
  "parameters": {
    "name": "test1",
    "network_url": "minio://sumo-data/test1/net.net.xml",
    "detectors_url": "minio://sumo-data/test1/detectors.json",
    "flow_interval_minutes": 15,
    "simulation_datetime": "2024-01-15T08:00:00",
    "result_url": "minio://sumo-data/test1/results/2024-01-15T08:00:00.xml"
  },
  "inputs": [
    {
      "name": "flow_data",
      "shape": [15, 3],
      "datatype": "INT32",
      "data": [
        1, 0, 143, 1, 15, 120, 1, 30, 87, 1, 45, 93, 1, 60, 58,
        2, 0, 143, 2, 15, 120, 2, 30, 87, 2, 45, 93, 2, 60, 58,
        3, 0, 143, 3, 15, 120, 3, 30, 87, 3, 45, 93, 3, 60, 58
      ]
    }
  ]
}
```

### Response
```json
{
  "model_name": "SUMO",
  "id": "test_run",
  "outputs": [
    {
      "name": "prediction",
      "shape": [1],
      "datatype": "BYTES",
      "data": ["{\"prediction\": \"minio://sumo-data/test1/results/2024-01-15T08:00:00.xml\"}"]
    }
  ]
}
```

## Asynchronous Mode (Batch Processing)

### Endpoint
```
POST /v1/jobs
```

### Request Schema

```json
{
  "samples": [
    {
      "detector_id": "integer",
      "time": "integer",
      "flow": "integer"
    }
  ],
  "csv_file_url": "string",
  "parameters": {
    "name": "string",
    "network_url": "string",
    "detectors_url": "string",
    "flow_interval": "integer",
    "simulation_datetime": "string",
    "result_url": "string"
  }
}
```

### Parameters
- **samples** (optional): Array of flow observations (use this OR csv_file_url)
- **csv_file_url** (optional): MinIO URL to CSV file with samples (use this OR samples)
- **parameters** (required): Simulation parameters object
  - **name** (required): Domain name for the simulation
  - **network_url** (required): MinIO URL to the SUMO network XML file
  - **detectors_url** (required): MinIO URL to the detectors JSON file
  - **flow_interval** (required): Time interval in minutes between flow observations
  - **simulation_datetime** (optional): ISO format datetime for simulation start
  - **result_url** (optional): MinIO URL where results will be uploaded

### Example Request with Direct Samples

```json
{
  "samples": [
    {"detector_id": 1, "time": 0, "flow": 143},
    {"detector_id": 1, "time": 15, "flow": 120},
    {"detector_id": 1, "time": 30, "flow": 87},
    {"detector_id": 1, "time": 45, "flow": 93},
    {"detector_id": 1, "time": 60, "flow": 58},
    {"detector_id": 2, "time": 0, "flow": 143},
    {"detector_id": 2, "time": 15, "flow": 120},
    {"detector_id": 2, "time": 30, "flow": 87},
    {"detector_id": 2, "time": 45, "flow": 93},
    {"detector_id": 2, "time": 60, "flow": 58}
  ],
  "parameters": {
    "name": "test1",
    "network_url": "minio://sumo-data/test1/net.net.xml",
    "detectors_url": "minio://sumo-data/test1/detectors.json",
    "flow_interval": 15,
    "simulation_datetime": "2024-01-15T08:00:00",
    "result_url": "minio://sumo-data/test1/results/2024-01-15T08:00:00.xml"
  }
}
```

### Example Request with CSV File

```json
{
  "csv_file_url": "minio://sumo-data/test1/flows.csv",
  "parameters": {
    "name": "test1",
    "network_url": "minio://sumo-data/test1/net.net.xml",
    "detectors_url": "minio://sumo-data/test1/detectors.json",
    "flow_interval": 15,
    "simulation_datetime": "2024-01-15T08:00:00",
    "result_url": "minio://sumo-data/test1/results/2024-01-15T08:00:00.xml"
  }
}
```

### Response
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "Batch prediction job submitted to queue",
  "created_at": "2024-01-15T08:00:00.000000"
}
```

### Job Status Endpoint
```
GET /v1/jobs/{job_id}
```

### Job Result Endpoint
```
GET /v1/jobs/{job_id}/result
```

### List Jobs Endpoint
```
GET /v1/jobs?status=completed&limit=100
```

### Cancel Job Endpoint
```
DELETE /v1/jobs/{job_id}
```

```csv
detector_id,time,flow,domain 
1,15,32,valencia_demo 
1,30,33,valencia_demo 
2,15,31,valencia_demo 
2,60,35,valencia_demo 
```