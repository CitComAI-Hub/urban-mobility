import os
import json
import shutil
import logging
import requests
import subprocess
import sys
import math
import xml.etree.ElementTree as ET
import pandas as pd
from datetime import datetime, timezone
from sumolib.net import readNet
from sumolib import sensors, files
from sumolib.geomhelper import distancePointToLine

from config import settings
from exceptions import SumoException
from ngsild_to_sumo_net import NgsildToSumoNet
from emissions import SumoPostprocessor

# MinIO, the Data Platform (Trino) and the NGSI-LD downloader/uploader are imported
# when used, so the controller also runs without the server's dependencies (e.g.
# from a batch job that brings its own network, detectors and flows).


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SumoController:
    """
    Controller for SUMO traffic simulations.

    Supports two modes for network retrieval:
    1. MinIO mode (default): Downloads pre-built net.xml from MinIO storage
    2. NGSI-LD mode: Downloads entities from NGSI-LD broker and converts to net.xml

    Example usage with NGSI-LD:
        controller = SumoController(
            name="my_domain",
            network_url="s3://bucket/network.xml",  # Not used in NGSI mode
            detectors_url="s3://bucket/detectors.json",
            flow_data=[[1, 0, 100], [1, 5, 120]],
            flow_interval_minutes=5,
            simulation_datetime="2024-01-01T00:00:00",
            use_ngsi_downloader=True,
            ngsi_domain="my_domain"  # Optional, defaults to 'name'
        )
        controller.run()

    Example usage with DPL:
        controller = SumoController(
            name="my_domain",
            network_url="s3://bucket/network.xml",  # Not used in NGSI mode
            detectors_url=None,  # Not used in DPL mode
            flow_data=None,  # Not used in DPL mode
            flow_interval_minutes=5,
            simulation_datetime="2024-01-01T00:00:00",
            use_ngsi_downloader=True,
            use_dpl_downloader=True,
            ngsi_domain="my_domain"
        )
        controller.run()
    """
    def __init__(
        self,
        name: str,
        network_url: str,
        detectors_url: str = None,
        flow_data: list = None,
        flow_interval_minutes: int = 15,
        simulation_datetime: str = None,
        use_ngsi_downloader: bool = False,
        ngsi_domain: str = None,
        use_dpl_downloader: bool = False,
        dpl_domain: str = None,
        dpl_schema: str = None,
        meso_simulation: bool = False,
        simulation_step: int = 60,
        upload_entities: bool = False,
        cleanup: bool = True,
        network_file: str = None,
        detectors: list = None,
        upload_results: bool = True,
        edge_emissions: bool = False,
        sumo_args: list = None,
        random_trips_args: list = None,
        route_sampler_args: list = None,
        candidate_routes_file: str = None,
    ):
        """New optional arguments (all default to the original behaviour):

        network_file: local SUMO network (.net.xml[.gz]) instead of MinIO / NGSI-LD.
        detectors: detectors as a list of {"detector_id", "lon", "lat"[, "heading"]}
            instead of detectors_url. With "heading" (direction of travel, degrees from
            north, clockwise) the detector goes on a lane that runs that way.
        upload_results: upload the result CSVs to MinIO (False: keep them local).
        edge_emissions: also aggregate emissions per edge and interval in SUMO
            (edgeData type="emissions", file edge_emissions_output), much lighter than
            the per-vehicle emission output for large networks.
        sumo_args, random_trips_args, route_sampler_args: extra command line options.
        candidate_routes_file: routes that routeSampler chooses from, computed once
            for the network (randomTrips with -r), instead of on every run: they do not
            depend on the counts, and computing many of them takes minutes.
        """
        self.name = name
        self.network_url = network_url
        self.detectors_url = detectors_url
        self.flow_data = flow_data
        if simulation_datetime is None:  # the sync KServe model does not send it
            simulation_datetime = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
        self.simulation_datetime = simulation_datetime.split('+')[0]
        self.simulation_minutes = 0
        self.use_ngsi_downloader = use_ngsi_downloader
        self.ngsi_domain = ngsi_domain or name
        self.use_dpl_downloader = use_dpl_downloader
        self.dpl_domain = dpl_domain or name
        self.dpl_schema = dpl_schema or settings.DPL_DEFAULT_SCHEMA
        self.flow_interval_minutes = flow_interval_minutes
        self.simulation_step = simulation_step
        self.edgedata_entities = []
        self.meso_simulation = meso_simulation
        self.upload_entities = upload_entities
        self.cleanup = cleanup
        self.local_network_file = network_file
        self.detectors = detectors
        self.upload_results = upload_results
        self.edge_emissions = edge_emissions
        self.sumo_args = list(sumo_args or [])
        self.random_trips_args = list(random_trips_args or [])
        self.route_sampler_args = list(route_sampler_args or [])
        self.candidate_routes_file = candidate_routes_file
        self.emission_url = self.edgedata_url = None

        # File definitions for SUMO
        self.domain_dir = os.path.join(settings.WORK_DIR, name)
        self.network_file = network_file or os.path.join(self.domain_dir, "net.net.xml")
        self.detectors_file = os.path.join(self.domain_dir, "detectors.det.xml")
        self.routes_file = os.path.join(self.domain_dir, "routes.rou.xml")
        self.flow_file = os.path.join(self.domain_dir, "flow.csv")
        self.edgedata_file = os.path.join(self.domain_dir, "edgedata.edg.xml")
        self.vehicles_file = os.path.join(self.domain_dir, "vehicles.rou.xml")
        self.edgedata_output = os.path.join(self.domain_dir, "edgedata.out.xml")
        self.queuedata_output = os.path.join(self.domain_dir, "queuedata.out.xml")
        self.ngsi_entities_file = os.path.join(self.domain_dir, "ngsi_entities.json")
        self.emission_file = os.path.join(self.domain_dir, "emission.xml")
        self.edgedata_ngsi_output = os.path.join(self.domain_dir, "edgedata_out.json")
        self.emission_csv = os.path.join(self.domain_dir, "emission.csv")
        self.edgedata_csv = os.path.join(self.domain_dir, "edgedata.csv")
        self.edge_emissions_additional = os.path.join(self.domain_dir, "edge_emissions.add.xml")
        self.edge_emissions_output = os.path.join(self.domain_dir, "edge_emissions.out.xml")

        # MinIO client to download input files and upload results
        self.minio_client = None
        if self.local_network_file is None or self.detectors is None or self.upload_results:
            from kafka_config import MinioClient
            self.minio_client = MinioClient()

        # NGSI-LD downloader and uploader
        self.ngsi_uploader = None
        if self.upload_entities:
            from ngsi_uploader import NGSIUploader
            self.ngsi_uploader = NGSIUploader()
        self.ngsi_downloader = None
        if self.use_ngsi_downloader:
            from ngsi_downloader import NGSILDDownloader
            self.ngsi_downloader = NGSILDDownloader()

        # DPL downloader
        self.dpl_downloader = None
        if self.use_dpl_downloader or (self.detectors is None and self.detectors_url is None):
            from dpl_downloader import DPLDownloader
            self.dpl_downloader = DPLDownloader()

    def run(self):
        """
        Executes SUMO and all intermediate steps.
        """
        try:
            logger.info(f"Starting simulation for domain {self.name}...")
            self.__create_domain()
            self.__process_detectors()
            self.__process_flow_data()
            if self.candidate_routes_file:
                self.routes_file = self.candidate_routes_file
            else:
                self.__random_trips()
            self.__generate_edgedata()
            self.__route_sampler()
            self.__sumo()
            self.__postprocess()
            self.emission_url, self.edgedata_url = self.__upload_results()
            logger.info(f"Simulation finished with success")
            return self.emission_url, self.edgedata_url

        except SumoException:
            logger.info("Simulation aborted. Bye!")

        finally:
            # before, a successful run returned without cleaning up
            if self.cleanup:
                self.__clean_files()

    def __postprocess(self):
        # The SumoEdgeData entities are only used to upload them
        if self.upload_entities:
            self.edgedata_entities = self.edgedata_to_ngsild()
        if not self.edge_emissions:
            self.__process_emissions()

    def __upload_results(self):
        if self.upload_entities:
            self.__upload_entities(self.edgedata_entities)
        if not self.upload_results:
            return None, None
        if self.edge_emissions:
            return None, self.__upload_edgedata_csv_free()
        emission_url = self.__upload_emissions()
        edgedata_url = self.__upload_edgedata()
        return emission_url, edgedata_url

    def __upload_edgedata_csv_free(self):
        """With edge_emissions the per-vehicle emissions are not processed: upload the
        raw edgedata output."""
        name = self.simulation_datetime.replace(' ', '') + "_edgedata.out.xml"
        return self.minio_client.upload_file(name, self.edgedata_output)

    def __process_emissions(self):
        emission_processor = SumoPostprocessor(
            self.emission_file, 
            self.edgedata_output,
            self.queuedata_output,
            self.simulation_datetime
        )
        emission_df, edgedata_df = emission_processor.postprocess()
        emission_df.to_csv(self.emission_csv, index=False)
        edgedata_df.to_csv(self.edgedata_csv, index=False)

    def __upload_edgedata(self):
        logger.info(f"Uploading edgedata for domain {self.name}")
        object_filename = self.simulation_datetime.replace(' ', '') + "_edgedata.csv"
        if self.network_url:
            object_name = self.network_url.split(self.minio_client.bucket)[-1]
            object_name = '/'.join(object_name.split('/')[:-1])
            object_name = object_name + f"/{object_filename}"
        else:
            object_name = object_filename
        return self.minio_client.upload_file(object_name, self.edgedata_csv)

    def __upload_emissions(self):
        logger.info(f"Uploading emissions for domain {self.name}")
        object_filename = self.simulation_datetime.replace(' ', '') + "_emissions.csv"
        if self.network_url:
            object_name = self.network_url.split(self.minio_client.bucket)[-1]
            object_name = '/'.join(object_name.split('/')[:-1])
            object_name = object_name + f"/{object_filename}"
        else:
            object_name = object_filename
        return self.minio_client.upload_file(object_name, self.emission_csv)

    def __upload_entities(self, entities: list):
        """Uploads NGSI-LD entities to data platform."""
        logger.info(f"Uploading entities for domain {self.name}")
        self.ngsi_uploader.batch_upsert(entities, batch_size=len(entities))
    
    def __clean_files(self):
        logger.info(f"Cleaning simulation directory")
        shutil.rmtree(self.domain_dir, ignore_errors=True)

        # Close DPL connection if it was used
        if self.dpl_downloader:
            self.dpl_downloader.close()

    def __create_domain(self):
        """
        Creates/Downloads the necesary files related to the simulation domain.
        If use_ngsi_downloader is True, downloads entities from NGSI-LD broker
        and converts them to SUMO net.xml format.
        Otherwise, downloads the network file from MinIO.
        """
        # Create domain directory if not exists
        if not os.path.exists(self.domain_dir):
            os.makedirs(self.domain_dir)

        # Retrieve domain information
        if self.local_network_file:
            if not os.path.exists(self.local_network_file):
                logger.error(f"Network file {self.local_network_file} not found")
                raise SumoException
        elif self.use_ngsi_downloader:
            logger.info(f"Downloading NGSI-LD entities for domain '{self.ngsi_domain}'...")
            try:
                # Download entities from NGSI-LD broker
                entities = self.ngsi_downloader.download_to_file(
                    output=self.ngsi_entities_file,
                    domain=self.ngsi_domain,
                )
                logger.info(f"Downloaded {len(entities)} NGSI-LD entities")

                # Convert NGSI-LD entities to SUMO net.xml
                logger.info(f"Converting NGSI-LD entities to SUMO network format...")
                NgsildToSumoNet.from_file(self.ngsi_entities_file).write(self.network_file)
                logger.info(f"Network file created at {self.network_file}")

            except Exception as e:
                logger.error(f"Error downloading or converting NGSI-LD entities: {e}")
                raise SumoException
        else:
            # Original behavior: download from MinIO
            object_name = self.network_url.split(f"{self.minio_client.bucket}/")[-1]
            self.minio_client.download_file(object_name, self.network_file)

    def __process_detectors(self):
        """
        Process the detector information given in the inference request and saves it to the
        detector file for SUMO.
        Downloads detectors from DPL if use_dpl_downloader is True, otherwise from MinIO.
        """
        # Download detectors
        if self.detectors is not None:
            detectors = self.detectors
        elif (self.use_dpl_downloader) or (self.detectors_url is None):
            logger.info(f"Downloading detectors from DPL for domain '{self.dpl_domain}'...")
            detectors = self.dpl_downloader.download_detectors(domain=self.dpl_domain, schema=self.dpl_schema)
        else:
            detectors = json.loads(
                self.minio_client.download_bytes(self.detectors_url)
            )

        # Read network
        network = readNet(self.network_file)

        # List to store SUMO detectors
        sumo_detectors = []

        skipped = 0
        for detector in detectors:
            detector_id, lon, lat = detector["detector_id"], detector["lon"], detector["lat"]
            heading = detector.get("heading")
            xy_pos = network.convertLonLat2XY(lon, lat)
            # With a direction of travel, a wider radius: the closest lane is often the
            # one of the opposite direction.
            radius = 10 if heading is None else 25
            lanes = network.getNeighboringLanes(xy_pos[0], xy_pos[1], radius)
            lanes = [lane for lane in lanes if "passenger" in lane[0].getPermissions()]
            if heading is not None:
                lanes = [(lane, dist) for lane, dist in lanes
                         if _angle_diff(_lane_bearing(lane, xy_pos), heading) < MAX_HEADING_DIFF]
            if not lanes:
                skipped += 1
                continue
            bestLane, _ = min(lanes, key=lambda item: item[1])
            # the position was computed inside the loop, for every candidate lane
            pos, _ = bestLane.getClosestLanePosAndDist(xy_pos)
            pos = min(max(pos, 0.0), bestLane.getLength() - 0.1)
            sumo_detectors.append(
                sensors.inductive_loop.InductiveLoop(str(detector_id), bestLane.getID(), pos)
            )
        if skipped:
            logger.warning(f"{skipped} detector(s) without a matching lane were left out")
        self.detector_ids = {d.id for d in sumo_detectors}
        files.additional.write(self.detectors_file, sumo_detectors)

    def __process_flow_data(self):
        """
        Converts the input flows from numpy array to a pandas DataFrame and stores it in the flow file.
        Downloads flow data from DPL if use_dpl_downloader is True, otherwise uses provided flow_data.
        """
        if self.use_dpl_downloader:
            logger.info(f"Downloading flow data from DPL for domain '{self.dpl_domain}'...")
            flow_data = self.dpl_downloader.download_flow_data(domain=self.dpl_domain, schema=self.dpl_schema)
        else:
            flow_data = self.flow_data

        df = pd.DataFrame(flow_data, columns=["Detector", "Time", "passenger"])
        df["Detector"] = df["Detector"].astype(int)
        df["Time"] = df["Time"].astype(int)
        # Only flows of detectors that were placed on the network
        if getattr(self, "detector_ids", None) is not None:
            df = df[df["Detector"].astype(str).isin(self.detector_ids)]
        # The interval is the step between the times of the data (the row order is not
        # guaranteed); with a single time, the one given in the request.
        times = sorted(df["Time"].unique())
        if len(times) > 1:
            self.flow_interval_minutes = int(min(b - a for a, b in zip(times, times[1:])))
        # The simulation ends when the last interval ends, not when it starts
        self.simulation_minutes = int(times[-1]) + int(self.flow_interval_minutes)
        df.to_csv(self.flow_file, sep=';', index=False)

    def __random_trips(self) -> bool:
        """
        Generates the routes that vehicles can follow in the simulation.
        """
        random_trips_cmd = [
            sys.executable,
            os.path.join(settings.SUMO_DIR, "tools", "randomTrips.py"),
            "-n",
            self.network_file,
            "-r",
            self.routes_file,
            "-o",
            os.path.join(self.domain_dir, "trips.trips.xml"),
            "-l",
            "-L",
            "--end",
            str(self.simulation_minutes * 60),
        ] + self.random_trips_args
        self.__run_cmd(random_trips_cmd)

    def __generate_edgedata(self) -> bool:
        """
        Converts flow observations to edgadata-format.
        """
        edgedata_script = os.path.join(settings.SUMO_DIR, "tools", "detector", "edgeDataFromFlow.py")
        edgedata_cmd = [
            sys.executable,
            edgedata_script,
            "-d",
            self.detectors_file,
            "-f",
            self.flow_file,
            "-q",
            "passenger",
            "-i",
            str(self.flow_interval_minutes),
            "-o",
            self.edgedata_file,
        ]
        self.__run_cmd(edgedata_cmd)

    def __route_sampler(self) -> bool:
        """
        Generates individual vehicle routes to match flow observations.
        """
        routesampler_script = os.path.join(settings.SUMO_DIR, "tools", "routeSampler.py")
        routesampler_cmd = [
            sys.executable,
            routesampler_script,
            "-r",
            self.routes_file,
            "--edgedata-files",
            self.edgedata_file,
            "--edgedata-attribute",
            "passenger",
            "--prefix",
            "passenger",
            "-o",
            self.vehicles_file,
            "--threads",
            str(settings.AVAILABLE_PROCESSORS),
        ] + self.route_sampler_args
        self.__run_cmd(routesampler_cmd)

    def __sumo(self) -> None:
        """
        Executes the main SUMO program and generates outputs.
        """
        sumo_cmd = [
            "sumo",
            "-n",
            self.network_file,
            "-r",
            self.vehicles_file,
            "--mesosim",
            str(self.meso_simulation).lower(),
            "--end",
            str(self.simulation_minutes * 60),
            "--queue-output",
            self.queuedata_output,
        ]
        if self.edge_emissions:
            # Traffic and emissions per edge and per interval of the counts (the
            # --edgedata-output option aggregates the whole simulation in one
            # interval, which mixes warm-up hours with the measured one), instead of
            # emissions per vehicle and step.
            period = self.flow_interval_minutes * 60
            with open(self.edge_emissions_additional, "w") as f:
                f.write(
                    '<additional>\n'
                    f'    <edgeData id="traffic" file="{self.edgedata_output}" period="{period}"/>\n'
                    f'    <edgeData id="emissions" type="emissions" file="{self.edge_emissions_output}" '
                    f'period="{period}" excludeEmpty="true"/>\n'
                    '</additional>\n'
                )
            sumo_cmd += ["-a", self.edge_emissions_additional, "--device.emissions.probability", "1"]
        else:
            sumo_cmd += [
                "--edgedata-output",
                self.edgedata_output,
                "--emission-output",
                self.emission_file,
                "--device.emissions.period",
                str(self.simulation_step),
            ]
        sumo_cmd += self.sumo_args
        self.__run_cmd(sumo_cmd)

    def edgedata_to_ngsild(self) -> list[dict]:
        """
        Converts the SUMO edgedata output (edgedata.out.xml) to NGSI-LD entities.

        Each edge in the output becomes a SumoEdgeData entity with:
        - location: GeoProperty LineString derived from the edge shape in the SUMO network.
        - interval begin/end: observedAt timestamps on each attribute.
        - All numeric edgedata attributes as individual NGSI-LD Properties.

        Returns the list of entities and also writes them to self.edgedata_ngsi_output.
        """
        CONTEXT = "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context-v1.8.jsonld"

        # Load SUMO network to get edge geometries
        network = readNet(self.network_file)

        # Parse the edgedata XML
        tree = ET.parse(self.edgedata_output)
        root = tree.getroot()

        entities = []

        for interval in root.findall("interval"):
            begin = float(interval.attrib.get("begin", 0))
            end = float(interval.attrib.get("end", 0))

            # Convert simulation end time to ISO8601 timestamp if simulation_datetime is set
            if self.simulation_datetime:
                sim_start = datetime.fromisoformat(self.simulation_datetime).replace(tzinfo=timezone.utc)
                t_end = datetime.fromtimestamp(
                    sim_start.timestamp() + end, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                t_end = f"{end:.2f}"

            for edge_elem in interval.findall("edge"):
                edge_id = edge_elem.attrib["id"]

                # Build entity skeleton
                pfx = f"urn:ngsi-ld:SumoEdgeData:{self.name}:"
                entity: dict = {
                    "@context": CONTEXT,
                    "id": f"{pfx}{edge_id}",
                    "type": "SumoEdgeData",
                    "domain": {"type": "Property", "value": self.name},
                    "edgeId": {"type": "Property", "value": edge_id},
                    "intervalBegin": {"type": "Property", "value": begin},
                    "intervalEnd": {"type": "Property", "value": end},
                }

                # Numeric attributes — attach observedAt so consumers know the time window
                skip_keys = {"id"}
                for attr_name, attr_val in edge_elem.attrib.items():
                    if attr_name in skip_keys:
                        continue
                    try:
                        numeric_val = float(attr_val)
                    except ValueError:
                        numeric_val = attr_val
                    entity[attr_name] = {
                        "type": "Property",
                        "value": numeric_val,
                        "observedAt": t_end,
                    }

                # GeoProperty: LineString from edge shape in the SUMO network
                try:
                    net_edge = network.getEdge(edge_id)
                    # getLanes() returns at least one lane; take shape from the edge itself
                    shape = net_edge.getShape()  # list of (x, y) in SUMO local coords
                    coords = [
                        list(network.convertXY2LonLat(x, y))
                        for x, y in shape
                    ]
                    if len(coords) >= 2:
                        entity["location"] = {
                            "type": "GeoProperty",
                            "value": {"type": "LineString", "coordinates": coords},
                        }
                except Exception:
                    # Edge not found in network (e.g. internal edge) — skip geometry
                    logger.warning(f"Edge with ID {edge_id} not found in network.")

                entities.append(entity)

        with open(self.edgedata_ngsi_output, "w", encoding="utf-8") as f:
            json.dump(entities, f, indent=2, ensure_ascii=False)

        logger.info(f"Wrote {len(entities)} SumoEdgeData NGSI-LD entities to {self.edgedata_ngsi_output}")
        return entities

    def __run_cmd(self, cmd: list):
        try:
            # an argument list, not a shell string: paths and names from requests are
            # not interpreted by a shell
            _ = subprocess.check_output([str(arg) for arg in cmd], stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as error:
            logger.error(f"Error executing command: \n{error}\n{error.stderr.decode(errors='replace')[-2000:]}")
            raise SumoException


MAX_HEADING_DIFF = 30  # degrees between a detector's direction of travel and its lane


def _angle_diff(a: float, b: float) -> float:
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def _lane_bearing(lane, xy_pos) -> float:
    """Direction of travel (degrees from north, clockwise) of the lane segment
    closest to a point, in the network's projected coordinates."""
    shape = lane.getShape()
    best = None
    for (x1, y1), (x2, y2) in zip(shape, shape[1:]):
        dist = distancePointToLine(xy_pos, (x1, y1), (x2, y2))
        if dist is not None and dist >= 0 and (best is None or dist < best[0]):
            best = (dist, x1, y1, x2, y2)
    if best is None:
        (x1, y1), (x2, y2) = shape[0], shape[-1]
    else:
        _, x1, y1, x2, y2 = best
    return math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360
