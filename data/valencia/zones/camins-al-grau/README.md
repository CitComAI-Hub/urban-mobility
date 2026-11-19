# Zone Camins al Grau – Poblats Marítims

Network and candidate routes of the Urban Mobility Digital Twin (`twin/`). See `zone.json`
for how they were built. To rebuild them from the city network (`v1.net.xml.gz` and its
`v1.candidate.rou.xml.gz`, built with `tools/network/build_network.py` and `tools/build_routes.sh`):

    netconvert -s v1.net.xml.gz --keep-edges.in-boundary <x0,y0,x1,y1 in net coordinates> -o net.xml.gz
    python $SUMO_HOME/tools/route/cutRoutes.py net.xml.gz v1.candidate.rou.xml.gz \
        --orig-net v1.net.xml.gz --min-length 2 -o candidate.rou.xml && gzip candidate.rou.xml

Calibration results (GEH < 5 on the detectors of the zone, 2026-11-18/19): 91 % at night,
69–78 % at the morning peak, 83 % at 10:00, 85 % at 13:00.
