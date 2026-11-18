import kserve
from model import SumoModel


if __name__ == "__main__":
    parser = kserve.model_server.parser
    args, _ = parser.parse_known_args()

    model = SumoModel(args.model_name)
    kserve.ModelServer().start([model])
