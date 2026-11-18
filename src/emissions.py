import os
import json
import pandas as pd
from datetime import datetime
from xml.etree import ElementTree as Et

EMISSION_ATTRIBUTES = ["CO2", "CO", "HC", "NOx", "PMx", "speed", "noise"]
ENTRY_MODEL = {
    "road_segment_id": "abc",
    "timestamp": "0000-01-01 00:00:00",
    "stats_per_class": {},
    "total_emission": {}
}


class SumoPostprocessor:
    def __init__(
        self, 
        emission_xml: str, 
        edgedata_output: str,
        queuedata_output: str,
        simulation_datetime: datetime
    ):
        self.simulation_datetime = simulation_datetime
        self.emission_xml = emission_xml
        self.edgedata_output = edgedata_output
        self.queuedata_output = queuedata_output

    @staticmethod
    def _read_xml(filepath: str):
        return Et.parse(filepath).getroot()
    
    def __get_emission_df(self) -> pd.DataFrame:
        emission_xml = self._read_xml(self.emission_xml)
        emission_df = pd.DataFrame(
            [
                [
                    float(timestep.attrib["time"]),
                    vehicle.attrib["lane"].split('_')[0],
                    vehicle.attrib["eclass"],
                    *[float(vehicle.attrib[col]) for col in EMISSION_ATTRIBUTES],
                ] for timestep in emission_xml for vehicle in timestep
            ],
            columns=["time", "edge", "eclass"]+EMISSION_ATTRIBUTES
        )
        t0 = pd.to_datetime(self.simulation_datetime)
        emission_df["time"] = t0 + pd.to_timedelta(emission_df["time"], 's')
        emission_df["time"] = emission_df["time"].dt.strftime("%Y-%m-%d %H:%M:%S")

        # Drop values with negative emission
        numeric_columns = emission_df.select_dtypes(include="number").columns
        emission_df = emission_df[(emission_df[numeric_columns] >= 0).all(axis=1)]

        # Convert emissions to gram / hour
        for column in EMISSION_ATTRIBUTES[:-2]:
            emission_df[column] *= 3600/1000

        # speed in km / h
        emission_df["speed_km_h"] = emission_df["speed"] * 3600 / 1000

        emission_df["CO2_grams_per_km"] = emission_df.apply(
            lambda row: row["CO2"] / row["speed_km_h"] if row["speed_km_h"] > 0 else 0, axis=1
        )

        # Delete outliers using IQR method
        # Remove outliers using IQR method
        Q1 = emission_df['CO2_grams_per_km'].quantile(0.25)
        Q3 = emission_df['CO2_grams_per_km'].quantile(0.75)
        IQR = Q3 - Q1
        lower_bound = Q1 - 1.5 * IQR
        upper_bound = Q3 + 1.5 * IQR

        emission_df = emission_df[(emission_df["CO2_grams_per_km"] >= lower_bound) & 
                                  (emission_df["CO2_grams_per_km"] <= upper_bound)]

        return emission_df

    def postprocess(self) -> pd.DataFrame:
        edgedata = self.process_edgedata()
        queuedata = self.process_queuedata()
        merged_data = pd.merge(edgedata, queuedata, on="id", how="outer")
        merged_data = merged_data.fillna(value=0.0)
        emission = self.__get_emission_df()
        return emission, merged_data

    @staticmethod
    def _to_float(value: any):
        try:
            return float(value)
        except ValueError:
            return value

    def __process_hourly_emissions(self, emission_df: pd.DataFrame) -> pd.DataFrame:
        """
        Processes a DataFrame containing emission data to calculate hourly totals
        for pollutants (CO2, CO, HC, NOx, PMx) and hourly means for speed and noise.

        Args:
            df (pd.DataFrame): The input DataFrame with emission data.
                            It must contain a 'time' column (convertible to datetime),
                            and columns for 'CO2', 'CO', 'HC', 'NOx', 'PMx', 'speed', and 'noise'.

        Returns:
            pd.DataFrame: A new DataFrame with data aggregated by hour, containing
                        total_CO2, total_CO, total_HC, total_NOx, total_PMx,
                        mean_speed, and mean_noise for each hour.
        """
        df = emission_df.copy()
        # Convert 'time' column to datetime objects
        df['time'] = pd.to_datetime(df['time'])

        # Set mins and seconds to zero for hourly aggregation
        df['hour'] = df['time'].dt.floor('H')

        # Define aggregation rules
        aggregation_rules = {
            'CO2': 'sum',
            'CO': 'sum',
            'HC': 'sum',
            'NOx': 'sum',
            'PMx': 'sum',
            'speed': 'mean',
            'noise': 'mean',
            'CO2_grams_per_km': 'mean',
        }

        # Group by hour and apply aggregation rules
        hourly_data = df.groupby('hour').agg(aggregation_rules)

        # Rename columns for clarity
        hourly_data = hourly_data.rename(columns={
            'CO2': 'total_CO2',
            'CO': 'total_CO',
            'HC': 'total_HC',
            'NOx': 'total_NOx',
            'PMx': 'total_PMx',
            'speed': 'mean_speed',
            'noise': 'mean_noise',
            'CO2_grams_per_km': 'mean_CO2_per_km_per_vehicle',
        })

        hourly_data["total_count"] = df.groupby(["hour"]).size()
        hourly_data["mean_CO2_per_hour_per_vehicle"] = hourly_data["total_CO2"] / hourly_data["total_count"]

        hourly_data.to_csv(self.emission_df_hourly_output, index=True)

        return hourly_data

    def __join_to_frontend(self, emission_df: pd.DataFrame) -> pd.DataFrame:
        # Read frontend file
        frontend_edges = pd.read_csv(self.frontend_file)["street_id"]

        # Empty dataframe to store the result of the operation
        frontend_results = pd.DataFrame()

        for front_edge in frontend_edges:
            # Get edge_id from street_id
            backend_edge_id = front_edge.split('_')[0]
            
            # Find edges that match the street_id
            backend_results = emission_df.loc[emission_df["edge"] == backend_edge_id].copy()
            
            # Assign value to the slice of the dataframe
            backend_results["street_id"] = front_edge
            frontend_results = pd.concat([frontend_results, backend_results], axis=0)

        return frontend_results
    
    def __process_emission_timeseries(self, emission_df: pd.DataFrame) -> dict:
        """
        Formats the emission data to a JSON. Firts, it is joined to the 
        frontend edges so that there is a value to all the visible entities in the domain.

        Args:
            hourly_emission_df (pd.DataFrame): _description_

        Returns:
            dict: _description_
        """
        # List to store the time series
        time_series = []

        # Join emission to frontend edges
        emission_df = self.__join_to_frontend(emission_df)
        
        # Convert time to pd.Datetime object
        emission_df["time"] = pd.to_datetime(emission_df["time"])
        
        # Drop 'eclass' column
        emission_df = emission_df.drop(columns="eclass")
        
        # Set time as index
        emission_df.set_index("time", inplace=True)

        emission_df = emission_df.rename(columns={
            'CO2': 'total_CO2',
            'CO': 'total_CO',
            'HC': 'total_HC',
            'NOx': 'total_NOx',
            'PMx': 'total_PMx',
            'speed': 'mean_speed',
            'noise': 'mean_noise',
            'CO2_grams_per_km': 'mean_CO2_per_km_per_vehicle',
        })

        aggregation_rules = {
            'total_CO2': 'sum',
            'total_CO': 'sum',
            'total_HC': 'sum',
            'total_NOx': 'sum',
            'total_PMx': 'sum',
            'mean_speed': 'mean',
            'mean_noise': 'mean',
            'mean_CO2_per_km_per_vehicle': 'mean',
        }
        
        for edge in emission_df.edge.unique():
            # Find slice of df with current street_id
            hourly_aggregate = emission_df.loc[emission_df.edge == edge].drop(columns=["edge", "street_id"])
            
            # Resample and aggregate the street values hourly
            hourly_aggregate = hourly_aggregate.resample('H').agg(aggregation_rules)

            # Write results for each pollutant
            for pollutant in hourly_aggregate.columns:
                time_series.append(
                    {
                        "device_id": f"urn:ngsi-ld:AirQualityObserved:Simulation_{self.domain}_{edge}",
                        "measure_id": f"{pollutant}",
                        "values": [
                            {"timestamp": index.strftime("%Y-%m-%d %H:%M:%S"), "value": value[pollutant]} 
                            for index, value in hourly_aggregate.iterrows()
                        ]
                    }
                )
        
        time_series = {"time_series": time_series}
        
        with open(self.emission_timeseries_output, 'w') as j:
            json.dump(time_series, j, indent=2)

        return time_series


    def __parse_edgedata(self) -> pd.DataFrame:
        edgedata_xml = self._read_xml(self.edgedata_output)
        edgedata_attributes = ["id", "density", "occupancy", "waitingTime", "speed"]
        edgedata_df = pd.DataFrame(
            [
                [subelement.attrib[attribute] for attribute in edgedata_attributes]
                for element in edgedata_xml for subelement in element
            ], columns=edgedata_attributes
        )
        return edgedata_df
    
    def process_edgedata(self) -> pd.DataFrame:
        edgedata = self.__parse_edgedata()
        for col in edgedata.columns[1:]:
            edgedata[col] = edgedata[col].apply(self._to_float)
        return edgedata
    
    def __parse_queuedata(self) -> pd.DataFrame:
        queuedata_xml = self._read_xml(self.queuedata_output)
        queuedata_attributes = ["id", "queueing_time", "queueing_length"]
        timesteps = []
        for ts in queuedata_xml:
            for lanes in ts:
                for lane in lanes:
                    timesteps.append([lane.attrib[att] for att in queuedata_attributes])
        queuedata_df = pd.DataFrame(timesteps, columns=queuedata_attributes)
        return queuedata_df
    
    def process_queuedata(self) -> pd.DataFrame:
        queuedata = self.__parse_queuedata()
        for col in queuedata.columns[1:]:
            queuedata[col] = queuedata[col].apply(self._to_float)
        queuedata = queuedata.groupby("id").mean().reset_index()
        queuedata["id"] = queuedata["id"].apply(lambda x: x.split('_')[0])
        queuedata = queuedata.groupby("id").max()
        return queuedata
