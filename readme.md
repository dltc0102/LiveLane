# LiveLane — A* Routing Engine for Hong Kong

An A* Routing Engine that finds the fastest route across Hong Kong by weighting road segments by travel time. It factors in road class, traffic-signal delays, toll costs, turn penalties, congestion factors, and live traffic speed data from Hong Kong Transport Department, with an interactive Folium map on a Stremalit frontend.

Reproducing the environment:
git clone, start up docker, `docker compose up --build -d`, it should load on your http://localhost:8501/.

--- 
Examples:
<img width="2409" height="1320" alt="image" src="https://github.com/user-attachments/assets/ef0fb9aa-86ce-4da2-8a5a-12caae7c2269" />

<img width="2460" height="1395" alt="image" src="https://github.com/user-attachments/assets/e966922b-199e-4ac6-a9df-a99a7bf97e8e" />

<img width="2457" height="1392" alt="image" src="https://github.com/user-attachments/assets/7e652520-afab-4b7e-9b80-81f058ecd321" />
