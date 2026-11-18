> **CitCom fork.** This repository is a fork of the [EU LDT Toolbox](https://interoperable-europe.ec.europa.eu/collection/ldttoolbox)
> module *Urban Mobility* ([eu_ldt_urban_mobility](https://code.europa.eu/ldt-toolbox/ai_models/eu_ldt_urban_mobility),
> commit `37fa5d3`, by Libelium Lab and Nunsys, EUPL-1.2), maintained by VRAIN (Universitat Politècnica de València) for the
> [CitCom data space](https://citcomai-hub.github.io/). It keeps the toolbox's SUMO pipeline and KServe server, fixes some of
> its bugs and adds a job that reads traffic counts **through a FIWARE data space** and publishes the results as **Smart Data
> Models**: see [CitCom data space job](#citcom-data-space-job) and [Changes from the toolbox](#changes-from-the-toolbox).

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

## CitCom data space job

`src/dataspace_job.py` (image `ghcr.io/citcomai-hub/urban-mobility`, built from `Dockerfile.job`) runs one simulation of the
last hour and exits. In the CitCom data space it runs every hour as a Kubernetes CronJob of the VRAIN participant:

```text
 Ajuntament de València (provider)                VRAIN (consumer)
 geoportal ──Airflow──▶ Orion-LD                   urban-mobility job
   traffic counts        TrafficFlowObserved  ◀── 1. token (ds-consumer-auth, MachineCredential)
                         (offering traffic-flow)    2. GET /ngsi-ld/v1/entities?type=TrafficFlowObserved
                                                     3. SumoController: detectors by direction of travel,
                                                        routeSampler to the counts, mesoscopic SUMO
                                                     4. upsert ──▶ VRAIN's NGSI-LD broker
                                                        TrafficFlowObserved (simulated) + CarbonFootprint
```

1. **Input, through the data space.** The traffic counts are Smart Data Models `TrafficFlowObserved` with `location` (point),
   `heading` (direction of travel, degrees from north, clockwise; a term of the SDM Transportation context), `intensity`
   (vehicles per hour) and `dateObserved`. The job gets a token for the provider's connector with
   [ds-consumer-auth](https://github.com/CitComAI-Hub/ds-consumer-auth): the consumer must have acquired the provider's offering
   in the marketplace. Counts older than `MAX_AGE_MINUTES`, or without position, direction or intensity, are left out.
2. **Simulation**, with the toolbox's `SumoController`: detectors placed on a lane that runs in the counted direction, candidate
   routes computed once for the network (`data/valencia/candidate.rou.xml.gz`), `routeSampler` to match the counts, mesoscopic
   SUMO with emissions aggregated per street.
3. **Output, Smart Data Models**, in the consumer's own broker, one entity per street with traffic:
   - `TrafficFlowObserved` (`urn:ngsi-ld:TrafficFlowObserved:<domain>:sim:<edge>`): `intensity` (veh/h),
     `averageVehicleSpeed` (km/h), `occupancy`, `dateObservedFrom`/`To`, `location` (LineString), `name` (street).
   - `CarbonFootprint` (`urn:ngsi-ld:CarbonFootprint:<domain>:sim:<edge>`): `CO2eq` (tonnes of CO2 in the hour, SUMO's
     emission model), `emissionSource: Transport`, `relatedSource` (the `TrafficFlowObserved`).

   Both say in `description` and `dataProvider` that they are simulated. Other pollutants have no fitting Smart Data Model
   (the toolbox published them as `AirQualityObserved`, a model for measured concentrations), so they are not published.

| Variable | Default | |
|---|---|---|
| `TRAFFIC_SOURCE_URL` | (required) | Provider's gateway, e.g. `https://apisix-townhall-vlc.citcom-vrain-dataspace.eu` |
| `OUTPUT_BROKER_URL` | (required) | NGSI-LD broker for the results |
| `DS_ISSUER_URL`, `DS_USERNAME`, `DS_PASSWORD`, `DS_VERIFIER_URL`, `DS_SCOPE`, `DS_CREDENTIAL_TYPE` | | ds-consumer-auth; without `DS_ISSUER_URL`, no token (local tests) |
| `NETWORK_FILE`, `CANDIDATE_ROUTES_FILE` | Valencia, in the image | SUMO network and candidate routes |
| `SIM_DOMAIN` | `valencia` | Part of the entity ids |
| `MAX_AGE_MINUTES` | `90` | Oldest count used |
| `MIN_DETECTORS` | `100` | Fewer usable counts: no simulation (exit 1) |
| `SUMO_THREADS` | CPUs | routeSampler threads |
| `UPSERT_BATCH_SIZE`, `UPSERT_PAUSE_SECONDS` | `50`, `0.5` | Entities per upsert and pause between batches (be gentle with small brokers) |
| `SUMO_ARGS`, `RANDOM_TRIPS_ARGS`, `ROUTE_SAMPLER_ARGS` | see `dataspace_job.py` | JSON lists of extra options |

Run it locally against any NGSI-LD broker with counts:

```console
docker build -f Dockerfile.job -t urban-mobility-job .
docker run --rm -e TRAFFIC_SOURCE_URL=http://broker:1026 -e OUTPUT_BROKER_URL=http://broker:1026 urban-mobility-job
```

Tests: `pip install -r requirements-job.txt pytest && python -m pytest tests`.

### Accuracy

First run (2026-11-18, 975 measurement points of the Ajuntament de València, one hour): the simulated vehicles on the streets
with detectors are 69% of the counted ones, and GEH < 5 holds on 46% of those streets (the usual target for a calibrated model
is 85%). routeSampler itself matches the counts much better (92% of the vehicles, GEH < 5 on 82% of the streets): the gap is the
network, built from OpenStreetMap and **not calibrated** (lanes, traffic light timings, junction capacities, entries at the
boundary), where the mesoscopic simulation still loses traffic. Treat the results as an estimate of where traffic and
emissions concentrate, not as values per street. The job logs this fit on every run.

Resources: ~110 s and ~3.6 GB of RAM per run (4 threads).

## Changes from the toolbox

Each change keeps the original behaviour by default, so that it can be proposed upstream.

- **Bugs fixed**
  - Synchronous KServe model: it read `controller.result_rul` (a typo; the result is now what `run()` returns) and did not
    pass `simulation_datetime`, which the controller split as a string (now the current time if missing).
  - `__process_detectors`: the lane position was computed inside the loop over candidate lanes; a detector without a
    lane crashed the run (now it is left out, with a warning).
  - `__process_flow_data`: the interval was `Time[1] - Time[0]` (0 with one row per detector, wrong if rows are not in
    time order) and the simulation ended when the last interval *started*.
  - `run()` returned before cleaning up after a successful simulation.
  - Commands ran through a shell (`shell=True` with a joined string): paths and names from requests were interpreted by
    the shell. Now they run as an argument list, and SUMO's tools with the current Python.
  - SUMO's tools were expected on the `PATH` and simulations were written inside `SUMO_DIR`: now `SUMO_HOME` and
    `SUMO_WORK_DIR`.
- **New options of `SumoController`**: local network file, detectors as a list (with an optional `heading`, to place them by
  direction of travel), no MinIO upload, emissions aggregated per edge in SUMO (much lighter than per vehicle), precomputed
  candidate routes, extra options for SUMO and its tools. MinIO, the Data Platform and the NGSI-LD downloader/uploader are
  imported only when used.
- **New**: `src/dataspace_job.py`, `src/sdm_output.py`, `Dockerfile.job` (SUMO from its official wheel, not the
  third-party `ngomariz/sumo-heroku` image), `data/valencia/` (OpenStreetMap network and candidate routes, ODbL),
  `tools/`, tests and CI.
- **Removed from this repository**: the password of an account of the toolbox's integration environment in
  `OIDC_ACCESS_SAMPLE_ADMIN.md` (the history was not imported for that reason).

Licence: EUPL-1.2, as the original. The network of Valencia is © OpenStreetMap contributors, ODbL (`data/valencia/README.md`).
