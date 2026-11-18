An A* routing engine for Hong Kong that weights roads by travel time, taking in considerations of road class, traffic-signal delays and live data from the Transport Department instead of distance with an interactive map visualiser. 

- Hong Kong road graph built from OSMnx data with assigned travel times per road edge
- Penalties added for nodes that have traffic lights so routes choose a longer road rather than a road with dense amounts of signals
- Routing visuals using an interactive map with Folium on Streamlit
