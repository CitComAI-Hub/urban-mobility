### Auth Steps

   ```bash
   export BASE_URL="https://dpl-int.ldttoolbox.app"
   export BASE_DQ_URL="https://dpl-dq-int.ldttoolbox.app"
   export OIDC_ENDPOINT=https://im-int.ldttoolbox.app/realms/LDT
   export OIDC_CLIENT=tool6
   export OIDC_USER=tool6_admin
   export OIDC_PASSWORD=<password>
   ```

2. Execute cURL command to get access token:

   ```bash
   export ACCESS_TOKEN=$(curl -s --location "${OIDC_ENDPOINT}/protocol/openid-connect/token" \
       --header "Content-Type: application/x-www-form-urlencoded" \
       --data-urlencode "grant_type=password" \
       --data-urlencode "client_id=${OIDC_CLIENT}" \
       --data-urlencode "username=${OIDC_USER}" \
       --data-urlencode "password=${OIDC_PASSWORD}" \
       --data-urlencode "scope=openid profile email" \ | jq -r '.access_token')
   ```

   ```bash
   echo $ACCESS_TOKEN
   ```

   - Check in [jwt.io](https://jwt.io)

3. Execute cURL command to get data with access token as authorization header:

   ```bash
   curl -s --insecure --location "${BASE_URL}/api/v1/entities?type=AirQualityObserved" \
   --header "Authorization: Bearer ${ACCESS_TOKEN}" | jq
   ```

### Authorization steps for context data

#### Authorization samples data platform backend

- **allow read** /api/v1/types 
  ```bash
  curl -s --insecure --location "${BASE_URL}/api/v1/types" \
  --header "scope: demo" \
  --header "Authorization: Bearer ${ACCESS_TOKEN}" | jq
  ```
- **allow write** /api/v1/entities 
  scope
  ```bash
  curl -s --insecure --request POST --location "${BASE_URL}/api/v1/entities" \
  --header "Authorization: Bearer ${ACCESS_TOKEN}" \
  --header "scope: demo" \
  --header "Content-Type: application/ld+json" \
  --data '{
    "id": "urn:ngsi-ld:AirQualityObserved:test-001",
    "type": "AirQualityObserved",
    "name": {
      "type": "Property",
      "value": "Test Air Quality Sensor"
    },
    "CO": {
      "type": "Property",
      "value": 500,
      "unitCode": "GQ"
    },
    "location": {
      "type": "GeoProperty",
      "value": {
        "type": "Point",
        "coordinates": [-3.703, 40.417]
      }
    },
    "@context": [
      "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context.jsonld"
    ]
  }'
  ```
- **allow read** /api/v1/entities
  ```bash
  curl -s --insecure --location "${BASE_URL}/api/v1/entities?type=AirQualityObserved" \
    --header "scope: demo" \
    --header "Authorization: Bearer ${ACCESS_TOKEN}" | jq
  ```

- **allow read** /cb/ngsi-ld/v1/types
  ```bash
  curl -s --insecure --location "${BASE_URL}/cb/ngsi-ld/v1/types" \
  --header "NGSILD-Tenant: demo" \
  --header "Authorization: Bearer ${ACCESS_TOKEN}" | jq
  ```
  
- **allow read** /cb/ngsi-ld/v1/entities?type=Road&q=priority>20 (important encode url of q param value)
  ```bash
  curl -s --insecure --location "${BASE_URL}/cb/ngsi-ld/v1/entities?type=Road&q=priority%3E20" \
  --header "NGSILD-Tenant: demo" \
  --header "Authorization: Bearer ${ACCESS_TOKEN}" | jq
  ```

- **allow write** /cb/ngsi-ld/v1/entityOperations/upsert because user has role "Admin"

  ```bash
  curl -s --insecure --request POST --location "${BASE_URL}/cb/ngsi-ld/v1/entityOperations/upsert" \
  --header "Authorization: Bearer ${ACCESS_TOKEN}" \
  --header "NGSILD-Tenant: scope2" \
  --header "Content-Type: application/ld+json" \
  --data '[{
    "id": "urn:ngsi-ld:AirQualityObserved:test-batch-01",
    "type": "AirQualityObserved",
    "name": {
      "type": "Property",
      "value": "Test Air Quality Sensor Batch 01"
    },
    "co": {
      "type": "Property",
      "value": 500,
      "unitCode": "GQ"
    },
    "location": {
      "type": "GeoProperty",
      "value": {
        "type": "Point",
        "coordinates": [-3.703, 40.417]
      }
    },
    "@context": [
      "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context.jsonld"
    ]
  },
  {
    "id": "urn:ngsi-ld:AirQualityObserved:test-batch-02",
    "type": "AirQualityObserved",
    "name": {
      "type": "Property",
      "value": "Test Air Quality Sensor Batch 02"
    },
    "co": {
      "type": "Property",
      "value": 500,
      "unitCode": "GQ"
    },
    "location": {
      "type": "GeoProperty",
      "value": {
        "type": "Point",
        "coordinates": [-3.703, 40.417]
      }
    },
    "@context": [
      "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context.jsonld"
    ]
  }]' | jq
  ```
