import os
from dotenv import load_dotenv
from dataclasses import dataclass

load_dotenv()


@dataclass
class Settings:
    # GENERAL CONFIG
    MODEL_NAME = "urban-flow"
    MODEL_VERSION = "0.1"

    # SUMO PARAMETERS
    # SUMO installation (its tools/ directory) and where each simulation writes its
    # files. SUMO_HOME is set by SUMO's images and by the eclipse-sumo wheel.
    SUMO_DIR = os.getenv("SUMO_HOME", "/var/lib/sumo")
    WORK_DIR = os.getenv("SUMO_WORK_DIR", os.getenv("SUMO_HOME", "/var/lib/sumo"))
    AVAILABLE_PROCESSORS = int(os.getenv("SUMO_THREADS", os.cpu_count() or 1))

    # DPL CONFIG
    DPL_HOST = os.getenv("DPL_HOST")
    DPL_PORT = os.getenv("DPL_PORT", 443)
    BASE_DQ_URL = os.getenv("BASE_DQ_URL")
    OIDC_ENDPOINT = os.getenv("OIDC_ENDPOINT")
    OIDC_CLIENT = os.getenv("OIDC_CLIENT")
    OIDC_USER = os.getenv("OIDC_USER")
    OIDC_PASSWORD = os.getenv("OIDC_PASSWORD")
    DPL_SCOPE = os.getenv("DPL_SCOPE")
    DPL_DEFAULT_SCHEMA = os.getenv("DPL_DEFAULT_SCHEMA", "ldt_data")

    # NGSI-LD BROKER CONFIG
    NGSI_BASE_URL = os.getenv("NGSI_BASE_URL")

settings = Settings()
