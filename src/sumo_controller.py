import os
import json
import shutil
import logging
import requests
import subprocess
import xml.etree.ElementTree as ET
import pandas as pd
from datetime import datetime, timezone
from sumolib.net import readNet
from sumolib import sensors, files

from config import settings
from exceptions import SumoException
from kafka_config import MinioClient
from ngsi_downloader import NGSILDDownloader
from ngsild_to_sumo_net import NgsildToSumoNet
from dpl_downloader import DPLDownloader
from emissions import SumoPostprocessor
from ngsi_uploader import NGSIUploader


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
    ):
        self.name = name
        self.network_url = network_url
        self.detectors_url = detectors_url
        self.flow_data = flow_data
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

        # File definitions for SUMO
        self.domain_dir = os.path.join(settings.SUMO_DIR, name)
        self.network_file = os.path.join(self.domain_dir, "net.net.xml")
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

        # MinIO client to download input files
        self.minio_client = MinioClient()

        # NGSI-LD downloader and uploader
        self.ngsi_uploader = NGSIUploader()
        self.ngsi_downloader = None
        if self.use_ngsi_downloader:
            self.ngsi_downloader = NGSILDDownloader()

        # DPL downloader
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

        if self.cleanup:
            self.__clean_files()

    def __postprocess(self):
        self.edgedata_entities = self.edgedata_to_ngsild()
        self.__process_emissions()

    def __upload_results(self):
        emission_url = self.__upload_emissions()
        edgedata_url = self.__upload_edgedata()
        if self.upload_entities:
            self.__upload_entities(self.edgedata_entities)
        return emission_url, edgedata_url

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
        shutil.rmtree(self.domain_dir)

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
        if self.use_ngsi_downloader:
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
        if (self.use_dpl_downloader) or (self.detectors_url is None):
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

        for detector in detectors:
            detector_id, lon, lat = detector["detector_id"], detector["lon"], detector["lat"]
            xy_pos = network.convertLonLat2XY(lon, lat)
            lanes = network.getNeighboringLanes(xy_pos[0], xy_pos[1], 10)
            lanes = [lane for lane in lanes if "passenger" in lane[0].getPermissions()]
            bestLane = None
            ref_d = 9999.0
            for lane, dist in lanes:
                if dist < ref_d:
                    ref_d = dist
                    bestLane = lane
                pos, _ = bestLane.getClosestLanePosAndDist(xy_pos)
            sumo_detectors.append(
                sensors.inductive_loop.InductiveLoop(detector_id, bestLane.getID(), pos)
            )
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
        self.simulation_minutes = df["Time"].max()
        self.flow_interval_minutes = df["Time"][1] - df["Time"][0]  # compute flow interval
        df.to_csv(self.flow_file, sep=';', index=False)

    def __random_trips(self) -> bool:
        """
        Generates the routes that vehicles can follow in the simulation.
        """
        random_trips_cmd = [
            "randomTrips.py",
            "-n",
            self.network_file,
            "-r",
            self.routes_file,
            "-o",
            os.path.join(self.domain_dir, "trips.trips.xml"),
            "-l",
            "-L",
        ]
        self.__run_cmd(random_trips_cmd)

    def __generate_edgedata(self) -> bool:
        """
        Converts flow observations to edgadata-format.
        """
        edgedata_script = os.path.join(settings.SUMO_DIR, "tools", "detector", "edgeDataFromFlow.py")
        edgedata_cmd = [
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
            str(settings.AVAILABLE_PROCESSORS)
        ]
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
            "--edgedata-output",
            self.edgedata_output,
            "--queue-output",
            self.queuedata_output,
            "--emission-output",
            self.emission_file,
            "--device.emissions.period",
            str(self.simulation_step),
        ]
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
            _ = subprocess.check_output(' '.join(cmd), stderr=subprocess.PIPE, shell=True)
        except subprocess.CalledProcessError as error:
            logger.error(f"Error executing command: \n{error}")
            raise SumoException
