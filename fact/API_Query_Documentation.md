# คู่มือการเข้าถึงและสืบค้นข้อมูล ArcGIS REST Services
# (API Query Documentation for Bangkok GIS Endpoints)

> เอกสารนี้จัดทำจากรายละเอียดชุดข้อมูลปัจจัยอื่น ๆ ที่มีผลต่อสุขภาวะ (เอกสารแนบ ๒)
> สืบค้นข้อมูล ณ วันที่ 8 เมษายน 2569

---

## สารบัญ (Table of Contents)

1. [ภาพรวมและหลักการเข้าถึง (Overview & Access Principles)](#1-ภาพรวมและหลักการเข้าถึง)
2. [สรุปสถานะการเข้าถึง (Access Status Summary)](#2-สรุปสถานะการเข้าถึง)
3. [วิธีการ Query ทั่วไปสำหรับ ArcGIS REST API](#3-วิธีการ-query-ทั่วไปสำหรับ-arcgis-rest-api)
4. [รายละเอียดแต่ละ Endpoint](#4-รายละเอียดแต่ละ-endpoint)
   - [๑ พื้นที่เขตการปกครอง](#endpoint-๑-พื้นที่เขตการปกครอง)
   - [๒ เส้นกึ่งกลางถนน](#endpoint-๒-เส้นกึ่งกลางถนน)
   - [๓ พื้นที่ถนน](#endpoint-๓-พื้นที่ถนน)
   - [๔ ท่าอากาศยาน](#endpoint-๔-ท่าอากาศยาน)
   - [๕ เส้นทางรถไฟฟ้า](#endpoint-๕-เส้นทางรถไฟฟ้า)
   - [๖ อาคาร](#endpoint-๖-อาคาร)
   - [๗ ขอบเขตชุมชน](#endpoint-๗-ขอบเขตชุมชน)
   - [๘ โรงพยาบาล](#endpoint-๘-โรงพยาบาล)
   - [๙ ศูนย์บริการสาธารณสุข](#endpoint-๙-ศูนย์บริการสาธารณสุข)
   - [๑๐ ศูนย์บริการสาธารณสุขสาขา](#endpoint-๑๐-ศูนย์บริการสาธารณสุขสาขา)
   - [๑๑ โรงควบคุมคุณภาพน้ำและบำบัดน้ำเสีย](#endpoint-๑๑-โรงควบคุมคุณภาพน้ำและบำบัดน้ำเสีย)
   - [๑๒ ศูนย์กำจัดขยะ](#endpoint-๑๒-ศูนย์กำจัดขยะ)
   - [๑๓ โรงเรียนสังกัด กทม](#endpoint-๑๓-โรงเรียนสังกัด-กทม)
   - [๑๔ โรงเรียนเอกชน](#endpoint-๑๔-โรงเรียนเอกชน)
   - [๑๕ โรงเรียนสังกัด สพฐ](#endpoint-๑๕-โรงเรียนสังกัด-สพฐ)
   - [๑๖ มหาวิทยาลัย](#endpoint-๑๖-มหาวิทยาลัย)
   - [๑๗ วิทยาลัย](#endpoint-๑๗-วิทยาลัย)
   - [๑๘ ตลาดที่ขึ้นทะเบียนกับกทม.](#endpoint-๑๘-ตลาดที่ขึ้นทะเบียนกับกทม)
   - [๑๙ ข้อมูลมลพิษทางอากาศ (PM2.5)](#endpoint-๑๙-ข้อมูลมลพิษทางอากาศ-pm25)
   - [๒๐ กิจการแพลนท์ปูน](#endpoint-๒๐-กิจการแพลนท์ปูน)
   - [๒๑ โครงการก่อสร้าง 50 เขต](#endpoint-๒๑-โครงการก่อสร้าง-50-เขต)
   - [๒๒ โรงงานที่รายงาน รว.3](#endpoint-๒๒-โรงงานที่รายงาน-รว3)
   - [๒๓ กิจการผลิต สะสม แบ่งบรรจุธูป](#endpoint-๒๓-กิจการผลิต-สะสม-แบ่งบรรจุธูป)
   - [๒๔ กิจการอู่พ่นสีรถยนต์](#endpoint-๒๔-กิจการอู่พ่นสีรถยนต์)
   - [๒๕ จุดตรวจวัดควันดำ](#endpoint-๒๕-จุดตรวจวัดควันดำ)
   - [๒๖ กิจการประดิษฐ์หินเป็นสิ่งของเครื่องใช้](#endpoint-๒๖-กิจการประดิษฐ์หินเป็นสิ่งของเครื่องใช้)
   - [๒๗ กิจการที่มีการใช้หม้อน้ำ](#endpoint-๒๗-กิจการที่มีการใช้หม้อน้ำ)
   - [๒๘ กิจการหลอมหรือหล่อโลหะ](#endpoint-๒๘-กิจการหลอมหรือหล่อโลหะ)
5. [ตัวอย่างการใช้งานขั้นสูง (Advanced Usage Examples)](#5-ตัวอย่างการใช้งานขั้นสูง)
6. [หมายเหตุและข้อจำกัด (Notes & Limitations)](#6-หมายเหตุและข้อจำกัด)

---

## 1. ภาพรวมและหลักการเข้าถึง

### Servers ที่เกี่ยวข้อง

| Server | Base URL | สถานะ |
|--------|----------|-------|
| cpudgiapp | `https://cpudgiapp.bangkok.go.th/arcgis/rest/services/` | ใช้งานได้ (ต้องใส่ `?f=json`) |
| cpudgiportal | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/` | Service ถูกลบหรือย้าย (404) |
| bmagis | `https://bmagis.bangkok.go.th/arcgis/rest/services/` | ใช้งานได้ |
| bmamap | `https://bmamap.bangkok.go.th/bmamap/rest/services/` | ใช้งานได้ |

### หลักการสำคัญ

1. **ต้องใส่ `?f=json`** — Server `cpudgiapp` จะคืน HTTP 403 หากเรียกแบบ HTML (ไม่ใส่ parameter `f`) แต่จะตอบ 200 OK เมื่อใส่ `?f=json` หรือ `?f=pjson`
2. **Query endpoint** — ทุก Layer ที่มี capability "Query" สามารถสืบค้นผ่าน `/query` endpoint ได้
3. **Output format** — ส่วนใหญ่รองรับ `JSON`, `geoJSON`, `PBF`
4. **Spatial Reference** — Layer จาก cpudgiapp ใช้ WKID 32647 (UTM Zone 47N), Layer จาก bmamap ใช้ WKID 4326 (WGS 84)

---

## 2. สรุปสถานะการเข้าถึง

| ลำดับ | ชั้นข้อมูล | สถานะ | Server | Geometry | WKID |
|------|-----------|-------|--------|----------|------|
| ๑ | พื้นที่เขตการปกครอง | **ใช้ได้** | cpudgiapp | Polygon | 32647 |
| ๒ | เส้นกึ่งกลางถนน | **ใช้ได้** | cpudgiapp | Polyline | 32647 |
| ๓ | พื้นที่ถนน | **ใช้ได้** | cpudgiapp | Polygon | 32647 |
| ๔ | ท่าอากาศยาน | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๕ | เส้นทางรถไฟฟ้า | **ใช้ได้** | cpudgiapp | Polyline | 32647 |
| ๖ | อาคาร | **ใช้ได้** | cpudgiapp | Polygon | 32647 |
| ๗ | ขอบเขตชุมชน | **ใช้ได้** | cpudgiapp | Polygon | 32647 |
| ๘ | โรงพยาบาล | **ใช้ได้** | bmagis | Point | 4326 |
| ๙ | ศูนย์บริการสาธารณสุข | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๑๐ | ศูนย์บริการสาธารณสุขสาขา | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๑๑ | โรงควบคุมคุณภาพน้ำฯ | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๑๒ | ศูนย์กำจัดขยะ | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๑๓ | โรงเรียนสังกัด กทม | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๑๔ | โรงเรียนเอกชน | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๑๕ | โรงเรียนสังกัด สพฐ | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๑๖ | มหาวิทยาลัย | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๑๗ | วิทยาลัย | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๑๘ | ตลาดที่ขึ้นทะเบียนกับกทม. | **URL เปลี่ยน** (พบ URL ใหม่) | cpudgiportal | Point | 32647 |
| ๑๙ | ข้อมูลมลพิษทางอากาศ (PM2.5) | **ใช้ได้** | bmagis | Point | 102100 |
| ๒๐ | กิจการแพลนท์ปูน | **ใช้ได้** | bmamap | Point | 4326 |
| ๒๑ | โครงการก่อสร้าง 50 เขต | **ใช้ได้** | bmamap | Point | 4326 |
| ๒๒ | โรงงานที่รายงาน รว.3 | **ใช้ได้** | bmamap | Point | 4326 |
| ๒๓ | กิจการผลิต สะสม แบ่งบรรจุธูป | **ใช้ได้** | bmamap | Point | 4326 |
| ๒๔ | กิจการอู่พ่นสีรถยนต์ | **ใช้ได้** | bmamap | Point | 4326 |
| ๒๕ | จุดตรวจวัดควันดำ | **ใช้ได้** | bmamap | Point | 4326 |
| ๒๖ | กิจการประดิษฐ์หินฯ | **ใช้ได้** | bmamap | Point | 4326 |
| ๒๗ | กิจการที่มีการใช้หม้อน้ำ | **ใช้ได้** | bmamap | Point | 4326 |
| ๒๘ | กิจการหลอมหรือหล่อโลหะ | **ใช้ได้** | bmamap | Point | 4326 |

> **สรุป:** ใช้งานได้ทั้ง **28 จาก 28 endpoints** — 17 endpoints ใช้ URL เดิมได้ทันที, อีก 11 endpoints บน `cpudgiportal` ที่ URL เดิมคืน 404 แต่พบว่าถูกย้ายไปรวมอยู่ใน service ใหม่ `GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer` โดยใช้ Layer ID เดิม

---

## 3. วิธีการ Query ทั่วไปสำหรับ ArcGIS REST API

### 3.1 ดูข้อมูล Metadata ของ Layer

เติม `?f=json` ต่อท้าย URL ของ Layer:

```bash
curl -s "<LAYER_URL>?f=json"
```

ตัวอย่าง:
```bash
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12?f=json"
```

เพื่อให้อ่านง่าย ใช้ `f=pjson` (pretty JSON):
```bash
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12?f=pjson"
```

### 3.2 Query Features (สืบค้นข้อมูล)

ใช้ endpoint `/query` ต่อท้าย Layer URL:

```
<LAYER_URL>/query?<parameters>
```

### 3.3 Query Parameters ที่สำคัญ

| Parameter | คำอธิบาย | ตัวอย่าง |
|-----------|---------|---------|
| `f` | Output format | `json`, `geojson`, `pjson`, `pbf` |
| `where` | SQL WHERE clause | `1=1` (ทั้งหมด), `DISTRICT_NAME_E='Bangrak'` |
| `outFields` | Fields ที่ต้องการ | `*` (ทั้งหมด), `NAME,ADDRESS` |
| `returnGeometry` | คืนค่า geometry หรือไม่ | `true`, `false` |
| `resultRecordCount` | จำนวน record ที่ต้องการ | `10`, `100` |
| `resultOffset` | เริ่มต้นที่ record ที่ | `0`, `100` (ใช้สำหรับ pagination) |
| `outSR` | Spatial Reference ของผลลัพธ์ | `4326` (WGS84 Lat/Lon) |
| `geometry` | Geometry สำหรับ spatial query | `100.5,13.7` (point), JSON envelope |
| `geometryType` | ชนิดของ geometry input | `esriGeometryPoint`, `esriGeometryEnvelope` |
| `spatialRel` | Spatial relationship | `esriSpatialRelIntersects`, `esriSpatialRelContains` |
| `inSR` | Spatial Reference ของ geometry input | `4326` |
| `orderByFields` | เรียงลำดับ | `NAME ASC`, `OBJECTID DESC` |
| `returnCountOnly` | คืนเฉพาะจำนวน | `true` |
| `returnDistinctValues` | คืนค่าที่ไม่ซ้ำ | `true` |
| `groupByFieldsForStatistics` | Group by | `DISTRICT_NAME_T` |
| `outStatistics` | Aggregate functions | `[{"statisticType":"count","onStatisticField":"OBJECTID","outStatisticFieldName":"total"}]` |

### 3.4 ตัวอย่าง curl พื้นฐาน

**ดึงข้อมูลทั้งหมด (จำกัด 5 records):**
```bash
curl -s "<LAYER_URL>/query?where=1%3D1&outFields=*&f=json&resultRecordCount=5"
```

**ดึงเฉพาะบาง fields โดยไม่เอา geometry:**
```bash
curl -s "<LAYER_URL>/query?where=1%3D1&outFields=NAME,ADDRESS&returnGeometry=false&f=json"
```

**นับจำนวน records ทั้งหมด:**
```bash
curl -s "<LAYER_URL>/query?where=1%3D1&returnCountOnly=true&f=json"
```

**Query ด้วยเงื่อนไข:**
```bash
curl -s "<LAYER_URL>/query?where=DNAME%3D'บางรัก'&outFields=*&f=json"
```

**ดึงข้อมูลในรูปแบบ GeoJSON (ใช้ในเครื่องมือ GIS ได้เลย):**
```bash
curl -s "<LAYER_URL>/query?where=1%3D1&outFields=*&f=geojson"
```

**Pagination (ดึงทีละ 100):**
```bash
# Page 1
curl -s "<LAYER_URL>/query?where=1%3D1&outFields=*&f=json&resultRecordCount=100&resultOffset=0"
# Page 2
curl -s "<LAYER_URL>/query?where=1%3D1&outFields=*&f=json&resultRecordCount=100&resultOffset=100"
```

**Spatial Query (ค้นหาภายในพื้นที่):**
```bash
curl -s "<LAYER_URL>/query?geometry=100.5,13.7&geometryType=esriGeometryPoint&inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=*&f=json"
```

**ใช้ Envelope (bounding box):**
```bash
curl -s "<LAYER_URL>/query?geometry={\"xmin\":100.4,\"ymin\":13.6,\"xmax\":100.6,\"ymax\":13.8,\"spatialReference\":{\"wkid\":4326}}&geometryType=esriGeometryEnvelope&spatialRel=esriSpatialRelIntersects&outFields=*&f=json"
```

### 3.5 ใช้งานผ่าน Python

```python
import requests

base_url = "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12/query"

params = {
    "where": "1=1",
    "outFields": "*",
    "returnGeometry": "true",
    "outSR": "4326",       # แปลง coordinate เป็น WGS84 Lat/Lon
    "f": "json",
    "resultRecordCount": 100,
    "resultOffset": 0
}

response = requests.get(base_url, params=params)
data = response.json()

for feature in data.get("features", []):
    attrs = feature["attributes"]
    print(attrs.get("DISTRICT_NAME_T"), attrs.get("SUBDISTRICT_NAME_T"))
```

### 3.6 ใช้งานผ่าน JavaScript (fetch)

```javascript
const baseUrl = "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12/query";

const params = new URLSearchParams({
  where: "1=1",
  outFields: "*",
  returnGeometry: true,
  outSR: 4326,
  f: "json",
  resultRecordCount: 100
});

const response = await fetch(`${baseUrl}?${params}`);
const data = await response.json();

data.features.forEach(f => {
  console.log(f.attributes.DISTRICT_NAME_T, f.attributes.SUBDISTRICT_NAME_T);
});
```

---

## 4. รายละเอียดแต่ละ Endpoint

---

### Endpoint ๑: พื้นที่เขตการปกครอง

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPolygon |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length |
|-----------|------|-------|--------|
| OBJECTID | OID | OBJECTID | - |
| ADMIN_BND_ID | String | ADMIN_BND_ID | 15 |
| DISTRICT_ID | String | DISTRICT_ID | 4 |
| SUBDISTRICT_CODE | String | SUBDISTRICT_CODE | 6 |
| SUBDISTRICT_NAME_T | String | SUBDISTRICT_NAME_T | 100 |
| SUBDISTRICT_NAME_E | String | SUBDISTRICT_NAME_E | 100 |
| DISTRICT_CODE | String | DISTRICT_CODE | 4 |
| DISTRICT_NAME_T | String | DISTRICT_NAME_T | 100 |
| DISTRICT_NAME_E | String | DISTRICT_NAME_E | 100 |
| CHANGWAT_CODE | String | CHANGWAT_Code | 2 |
| CHANGWAT_NAME_T | String | CHANGWAT_NAME_T | 100 |
| CHANGWAT_NAME_E | String | CHANGWAT_NAME_E | 100 |
| AREA_BMA | Double | AREA_BMA | - |
| REMARK | String | REMARK | 255 |
| UPDATE_DATA | Integer | UPDATE_DATA | - |
| SHAPE | Geometry | SHAPE | - |
| SHAPE.AREA | Double | SHAPE.AREA | - |
| SHAPE.LEN | Double | SHAPE.LEN | - |

#### ตัวอย่างการ Query

```bash
# ดึงข้อมูลเขตทั้งหมด (เฉพาะชื่อ ไม่เอา geometry)
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12/query?where=1%3D1&outFields=DISTRICT_NAME_T,DISTRICT_NAME_E,SUBDISTRICT_NAME_T,SUBDISTRICT_NAME_E&returnGeometry=false&f=json"

# ค้นหาเขตบางรัก
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12/query?where=DISTRICT_NAME_E%3D'Bangrak'&outFields=*&f=json"

# นับจำนวนแขวงทั้งหมด
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12/query?where=1%3D1&returnCountOnly=true&f=json"

# ดึง GeoJSON พร้อมแปลง coordinate เป็น WGS84
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"

# สถิติ: จำนวนแขวงต่อเขต
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12/query?where=1%3D1&groupByFieldsForStatistics=DISTRICT_NAME_T&outStatistics=%5B%7B%22statisticType%22%3A%22count%22%2C%22onStatisticField%22%3A%22OBJECTID%22%2C%22outStatisticFieldName%22%3A%22subdistrict_count%22%7D%5D&f=json"
```

---

### Endpoint ๒: เส้นกึ่งกลางถนน

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/7` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPolyline |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| UNIQUE_ID | String | Unique_ID | 15 | รหัสเฉพาะ |
| FNODE | Integer | Fnode | - | Node เริ่มต้น |
| TNODE | Integer | Tnode | - | Node ปลายทาง |
| ROAD_CL_ID | String | Road_CL_ID | 10 | รหัสเส้นกึ่งกลางถนน |
| ROAD_NAME_T | String | Road_Name_T | 100 | ชื่อถนน (ไทย) |
| ROAD_NAME_E | String | Road_Name_E | 100 | ชื่อถนน (อังกฤษ) |
| ROAD_DIRECTION | String | ROAD_DIRECTION | 1 | ทิศทางถนน |
| RC_LTYPE | String | RC_LTYPE | 2 | ประเภทเส้น |
| RC_LNUM | Integer | RC_LNUM | - | หมายเลขเส้น |
| OWNER_ORG | String | OWNER_ORG | 1 | หน่วยงานเจ้าของ |
| ROAD_CLASS | String | ROAD_CLASS | 1 | ชั้นถนน |
| RC_FUNC | String | RC_FUNC | 1 | ประเภทการใช้งาน |
| RC_LENGTH | Double | RC_LENGTH | - | ความยาว |
| RC_CODE | String | RC_CODE | 4 | รหัสถนน |
| RC_SPEED | String | RC_SPEED | 2 | ความเร็ว |
| RC_CAPACITY | Double | RC_CAPACITY | - | ความจุ |
| RC_LANE | String | RC_LANE | 2 | จำนวนเลน |
| RC_RWIDTH | Double | RC_RWIDTH | - | ความกว้างถนน |
| RC_SWIDTH | Double | RC_SWIDTH | - | ความกว้างทางเท้า |
| RC_TWIDTH | Double | RC_TWIDTH | - | ความกว้างรวม |
| RC_MTYPE | String | RC_MTYPE | 2 | ประเภทเกาะกลาง |
| RC_MWIDTH | Double | RC_MWIDTH | - | ความกว้างเกาะกลาง |
| RC_DATE | Date | RC_DATE | - | วันที่ปรับปรุง |
| TS_STATION | String | TS_STATION | 4 | สถานี |
| RC_DIRECTION | Integer | RC_DIRECTION | - | ทิศทาง |
| B_WIDTH | Double | B_WIDTH | - | ความกว้างสะพาน |
| B_DIRECTION | String | B_DIRECTION | 2 | ทิศทางสะพาน |
| B_MAT | String | B_MAT | 2 | วัสดุสะพาน |
| S_WIDTH | Double | S_WIDTH | - | ความกว้าง (S) |
| S_DIRECTION | String | S_DIRECTION | 2 | ทิศทาง (S) |
| S_MAT | String | S_MAT | 2 | วัสดุ (S) |
| F_LANES | Double | F_LANES | - | จำนวนเลน (F) |
| F_WIDTH | Double | F_WIDTH | - | ความกว้าง (F) |
| F_DIRECTION | String | F_DIRECTION | 2 | ทิศทาง (F) |
| F_MAT | String | F_MAT | 2 | วัสดุ (F) |
| REMARK | String | REMARK | 255 | หมายเหตุ |
| UPDATE_DATA | Integer | UPDATE_DATA | - | ปีที่ปรับปรุง |
| RC_DRIVETIME | Double | RC_DRIVETIME | - | เวลาขับผ่าน |
| RC_MAT | String | RC_MAT | 2 | วัสดุผิวถนน |
| SHAPE | Geometry | SHAPE | - | |
| SHAPE.LEN | Double | SHAPE.LEN | - | ความยาวรูปทรง |

#### ตัวอย่างการ Query

```bash
# ค้นหาถนนตามชื่อ
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/7/query?where=ROAD_NAME_E+LIKE+'%25Silom%25'&outFields=ROAD_NAME_T,ROAD_NAME_E,RC_LENGTH,RC_LANE&returnGeometry=false&f=json"

# ดึงถนนทั้งหมดในรูปแบบ GeoJSON
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/7/query?where=1%3D1&outFields=ROAD_NAME_T,ROAD_NAME_E&outSR=4326&f=geojson&resultRecordCount=100"

# นับจำนวนถนนทั้งหมด
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/7/query?where=1%3D1&returnCountOnly=true&f=json"
```

---

### Endpoint ๓: พื้นที่ถนน

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/9` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPolygon |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length |
|-----------|------|-------|--------|
| OBJECTID | OID | OBJECTID | - |
| ROADEDGE_BND_ID | String | RoadEdge_BND_ID | 15 |
| ROAD_CL_ID | String | Road_CL_ID | 10 |
| RDE_USE | Integer | RDE_USE | - |
| REMARK | String | REMARK | 255 |
| UPDATE_DATA | Integer | UPDATE_DATA | - |
| SHAPE | Geometry | SHAPE | - |
| SHAPE.AREA | Double | SHAPE.AREA | - |
| SHAPE.LEN | Double | SHAPE.LEN | - |

> **หมายเหตุ:** Field `ROAD_CL_ID` สามารถใช้ join กับ Endpoint ๒ (เส้นกึ่งกลางถนน) เพื่อได้ชื่อถนน

#### ตัวอย่างการ Query

```bash
# ดึงพื้นที่ถนนทั้งหมด (5 records แรก)
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/9/query?where=1%3D1&outFields=*&f=json&resultRecordCount=5"

# Spatial query: ถนนในพื้นที่กำหนด (envelope)
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/9/query?geometry=%7B%22xmin%22%3A670000%2C%22ymin%22%3A1520000%2C%22xmax%22%3A680000%2C%22ymax%22%3A1530000%2C%22spatialReference%22%3A%7B%22wkid%22%3A32647%7D%7D&geometryType=esriGeometryEnvelope&spatialRel=esriSpatialRelIntersects&outFields=*&f=json"
```

---

### Endpoint ๔: ท่าอากาศยาน

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิภาคารคมนาคมขนส่ง/MapServer/43` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/43` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### สิ่งที่ทดสอบแล้วกับ URL เดิม

| วิธีที่ทดสอบ | ผลลัพธ์ |
|-------------|--------|
| `?f=json` ต่อท้าย URL เดิม | HTTP 200 แต่ body คืน `{"error":{"code":404,"message":"Service not found"}}` |
| `?f=pjson` | เหมือนกัน — 404 ใน JSON body |
| ไม่ใส่ parameter `f` (HTML) | HTTP 404 |
| เติม browser User-Agent + Referer header | HTTP 200 แต่ body ยังคืน 404 error |
| เรียก MapServer root (ไม่ใส่ layer ID) | 404 — ทั้ง MapServer ถูกลบ |
| เรียก `/query` endpoint โดยตรง | 404 — service ไม่มีอยู่แล้ว |
| เปลี่ยนเป็น FeatureServer | 404 |
| ตรวจสอบ service directory `GI_Platform?f=json` | พบว่า folder มีแค่ 2 services เท่านั้น — service เดิมถูกลบไปแล้ว |
| สำรวจ root services directory | พบ service ใหม่ `GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer` ที่มี Layer 43 ชื่อ "ท่าอากาศยาน" |

#### สาเหตุ

Service เดิม `GI_Platform/แผนที่ภูมิภาคารคมนาคมขนส่ง` ถูก**ลบออกจาก server** และรวมเข้ากับ service ใหม่ `GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน` โดยยังคงใช้ Layer ID เดิม (43)

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | FID | - | |
| ID | Integer | ID | - | รหัส |
| NAME | String | NAME | 80 | ชื่อท่าอากาศยาน |
| DCODE | String | DCODE | 4 | รหัสเขต |
| TEL_ | String | TEL_ | 20 | เบอร์โทร |
| CPUDGITHEMATIC.AIRPORT.AREA | String | AREA | 50 | พื้นที่ |
| LOCATION | String | LOCATION | 100 | ที่ตั้ง |
| USABILITY | String | USABILITY | 50 | สถานะการใช้งาน |
| X | Double | X | - | พิกัด X |
| Y | Double | Y | - | พิกัด Y |
| URL | String | URL | 200 | URL ข้อมูลเพิ่มเติม |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
# ดึงข้อมูลท่าอากาศยานทั้งหมด
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/43/query?where=1%3D1&outFields=NAME,LOCATION,USABILITY,TEL_&returnGeometry=false&f=json"

# ดึงเป็น GeoJSON พร้อมแปลง coordinate เป็น WGS84
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/43/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"
```

---

### Endpoint ๕: เส้นทางรถไฟฟ้า

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://cpudgiapp.bangkok.go.th/arcgis/rest/services/EXTERNAL/Basemap_Traffic/MapServer/4` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPolyline |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Query, Map, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| SHAPE.LEN | Double | SHAPE.LEN | - | ความยาวรูปทรง |
| SHAPE | Geometry | SHAPE | - | |
| COLOR_T | String | สี | 50 | สีสาย (ไทย) |
| COLOR_E | String | สี ภาษาอังกฤษ | 50 | สีสาย (อังกฤษ) |
| ROUTE | String | เส้นทาง | 80 | เส้นทาง |
| STATUS | String | สถานะการให้บริการ | 50 | สถานะ (เปิด/ปิด/กำลังก่อสร้าง) |
| OWNER | String | ผู้ดูแล | 50 | หน่วยงานผู้ดูแล |
| SHAPE_LENG | Double | SHAPE_LENG | - | ความยาว |

#### ตัวอย่างการ Query

```bash
# ดึงเส้นทางรถไฟฟ้าทั้งหมด
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/EXTERNAL/Basemap_Traffic/MapServer/4/query?where=1%3D1&outFields=COLOR_T,COLOR_E,ROUTE,STATUS,OWNER&returnGeometry=false&f=json"

# ค้นหาเฉพาะสายสีเขียว
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/EXTERNAL/Basemap_Traffic/MapServer/4/query?where=COLOR_E+LIKE+'%25Green%25'&outFields=*&f=json"

# ดึงเป็น GeoJSON (พร้อมแปลงเป็น WGS84)
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/EXTERNAL/Basemap_Traffic/MapServer/4/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"
```

---

### Endpoint ๖: อาคาร

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/5` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPolygon |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| BLDG_ID | String | BLDG_ID | 15 | รหัสอาคาร |
| BL_TYPE | String | BL_TYPE | 2 | ประเภทอาคาร |
| BL_FRONTAGE | Double | BL_FRONTAGE | - | ด้านหน้า |
| BL_HEIGHT | Double | BL_HEIGHT | - | ความสูง |
| BL_DEPTH | Double | BL_DEPTH | - | ความลึก |
| BL_NSTOREY | String | BL_NSTOREY | 2 | จำนวนชั้น |
| BL_NUNIT | String | BL_NUNIT | 4 | จำนวนยูนิต |
| BL_UNIT_F | String | BL_UNIT_F | 1 | สถานะยูนิต |
| BL_NRESIDENT | Integer | BL_NRESIDENT | - | จำนวนผู้อยู่อาศัย |
| BL_EMPLOY | Integer | BL_EMPLOY | - | จำนวนพนักงาน |
| BL_OWNER | Integer | BL_OWNER | - | เจ้าของ |
| BL_AREA | Double | BL_AREA | - | พื้นที่ |
| BL_AREA_FLAG | String | BL_AREA_FLAG | 1 | สถานะพื้นที่ |
| BL_TAX_ID | Integer | BL_TAX_ID | - | รหัสภาษี |
| PRJ_ID | Integer | PRJ_ID | - | รหัสโครงการ |
| BL_USE | Integer | BL_USE | - | ประเภทการใช้ |
| BL_MATL | Integer | BL_MATL | - | วัสดุอาคาร |
| BL_NAME_T | String | BL_NAME_T | 100 | ชื่ออาคาร (ไทย) |
| BL_NAME_E | String | BL_NAME_E | 100 | ชื่ออาคาร (อังกฤษ) |
| BL_HID | String | BL_HID | 10 | รหัสบ้าน |
| BL_HOUSENUM | String | BL_HOUSENUM | 10 | เลขที่บ้าน |
| BL_VILLNUM | String | BL_VILLNUM | 10 | หมู่ |
| BL_VILLAGE | String | BL_VILLAGE | 35 | หมู่บ้าน |
| BL_ROAD | String | BL_ROAD | 150 | ถนน |
| BL_SUBDISTRICT | String | BL_SUBDISTRICT | 30 | แขวง |
| BL_DISTRICT | String | BL_DISTRICT | 30 | เขต |
| BL_CHANGWAT | String | BL_CHANGWAT | 30 | จังหวัด |
| BL_POSTCODE | String | BL_POSTCODE | 5 | รหัสไปรษณีย์ |
| BL_ADDRESS | String | BL_ADDRESS | 180 | ที่อยู่เต็ม |
| BL_ACT_MAJOR | String | BL_ACT_MAJOR | 45 | กิจกรรมหลัก |
| BL_ACT_MINOR | String | BL_ACT_MINOR | 45 | กิจกรรมรอง |
| BL_ACT_OTHER | String | BL_ACT_OTHER | 45 | กิจกรรมอื่น |
| BL_DATE | Integer | BL_DATE | - | วันที่ |
| REMARK | String | REMARK | 255 | หมายเหตุ |
| UPDATE_DATA | Integer | UPDATE_DATA | - | ปีที่ปรับปรุง |
| MATCHING | String | MATCHING | 1 | การจับคู่ |
| SHAPE | Geometry | SHAPE | - | |
| SHAPE.AREA | Double | SHAPE.AREA | - | พื้นที่รูปทรง |
| SHAPE.LEN | Double | SHAPE.LEN | - | ความยาวรูปทรง |

#### ตัวอย่างการ Query

```bash
# ค้นหาอาคารตามชื่อ
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/5/query?where=BL_NAME_E+LIKE+'%25Hospital%25'&outFields=BL_NAME_T,BL_NAME_E,BL_ADDRESS,BL_NSTOREY&returnGeometry=false&f=json"

# ค้นหาอาคารในเขตบางรัก
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/5/query?where=BL_DISTRICT%3D'บางรัก'&outFields=BL_NAME_T,BL_ADDRESS&returnGeometry=false&f=json&resultRecordCount=10"

# นับจำนวนอาคารทั้งหมด
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/5/query?where=1%3D1&returnCountOnly=true&f=json"
```

---

### Endpoint ๗: ขอบเขตชุมชน

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Community/Service_Community/FeatureServer/14` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPolygon |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Query, Create, Update, Delete, Uploads, Editing |
| **Max Record Count** | 1,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

> **หมายเหตุ:** นี่เป็น FeatureServer ที่รองรับ editing ด้วย (Create, Update, Delete) — ไม่ใช่แค่อ่านอย่างเดียว

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| Shape__Area | Double | SHAPE.AREA | - | พื้นที่ |
| Shape__Length | Double | SHAPE.LEN | - | ความยาวเส้นรอบรูป |
| COMMU_ADMIN_COMMU_BND_FINAL_CM | Double | รหัสชุมชน | - | รหัสชุมชน |
| COMMU_ADMIN_COMMU_BND_FINAL__1 | String | ชื่อชุมชน | 255 | ชื่อชุมชน |
| CMT_TYPE | String | ประเภท | 50 | ประเภทชุมชน |
| DNAME | String | เขต | 51 | เขต |
| SNAME | String | แขวง | 50 | แขวง |
| NHOUSE | Double | จำนวนบ้าน | - | จำนวนบ้าน |
| RAI | Double | ไร่ | - | ไร่ |
| NGAN | Double | งาน | - | งาน |
| WA | Double | วา | - | ตารางวา |
| HOUSEHOLD | Double | จำนวนครัวเรือน | - | จำนวนครัวเรือน |
| MALE | Double | ประชากรชาย | - | ประชากรชาย |
| FEMALE | Double | ประชากรหญิง | - | ประชากรหญิง |
| ADDRESS | String | ที่อยู่ | 254 | ที่อยู่ |
| NORTH_BND | String | ทิศเหนือติดกับ | 254 | ขอบเขตทิศเหนือ |
| SOUTH_BND | String | ทิศใต้ติดกับ | 254 | ขอบเขตทิศใต้ |
| EAST_BND | String | ทิศตะวันออกติดกับ | 254 | ขอบเขตทิศตะวันออก |
| WEST_BND | String | ทิศตะวันตกติดกับ | 254 | ขอบเขตทิศตะวันตก |
| REMARK | String | จุดสังเกต | 254 | หมายเหตุ/จุดสังเกต |
| LON | Double | ลองจิจูด | - | ลองจิจูด |
| LAT | Double | ละติจูด | - | ละติจูด |
| CHAIRMAN | String | ชื่อประธาน | 123 | ประธานชุมชน |
| STATUS | SmallInteger | STATUS | - | สถานะ |
| CREATED_USER | String | CREATED_USER | 255 | ผู้สร้าง |
| CREATED_DATE | Date | CREATED_DATE | - | วันที่สร้าง |
| LAST_EDITED_USER | String | LAST_EDITED_USER | 255 | ผู้แก้ไขล่าสุด |
| LAST_EDITED_DATE | Date | LAST_EDITED_DATE | - | วันที่แก้ไขล่าสุด |
| ESTABLISHED | Date | วันที่อนุมัติจัดตั้งชุมชน | - | วันที่จัดตั้ง |

#### ตัวอย่างการ Query

```bash
# ดึงชุมชนทั้งหมด (เฉพาะชื่อและเขต)
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Community/Service_Community/FeatureServer/14/query?where=1%3D1&outFields=COMMU_ADMIN_COMMU_BND_FINAL__1,DNAME,SNAME,HOUSEHOLD,MALE,FEMALE&returnGeometry=false&f=json&resultRecordCount=20"

# ค้นหาชุมชนในเขตบางซื่อ
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Community/Service_Community/FeatureServer/14/query?where=DNAME+LIKE+'%25บางซื่อ%25'&outFields=*&returnGeometry=false&f=json"

# นับจำนวนชุมชนทั้งหมด
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Community/Service_Community/FeatureServer/14/query?where=1%3D1&returnCountOnly=true&f=json"

# สถิติ: จำนวนชุมชนต่อเขต
curl -s "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Community/Service_Community/FeatureServer/14/query?where=1%3D1&groupByFieldsForStatistics=DNAME&outStatistics=%5B%7B%22statisticType%22%3A%22count%22%2C%22onStatisticField%22%3A%22OBJECTID%22%2C%22outStatisticFieldName%22%3A%22community_count%22%7D%5D&f=json"
```

---

### Endpoint ๘: โรงพยาบาล

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmagis.bangkok.go.th/arcgis/rest/services/จุดสนับสนุนสถานที่/MapServer/16` |
| **ArcGIS Version** | 10.71 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 4326 (WGS 84) — extent, Source WKID 32647 |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| NAME | String | ชื่อโรงพยาบาล | 60 | ชื่อโรงพยาบาล |
| DCODE | String | รหัสเขต | 4 | รหัสเขต |
| ADDRESS | String | ที่อยู่ | 110 | ที่อยู่ |
| TEL | String | เบอร์โทรศัพท์ | 20 | เบอร์โทร |
| NUM_BED | SmallInteger | จำนวนเตียง | - | จำนวนเตียง |
| X | Double | X | - | พิกัด X |
| Y | Double | Y | - | พิกัด Y |
| SHAPE | Geometry | Shape | - | |

#### ตัวอย่างการ Query

```bash
# ดึงรายชื่อโรงพยาบาลทั้งหมด
curl -s "https://bmagis.bangkok.go.th/arcgis/rest/services/%E0%B8%88%E0%B8%B8%E0%B8%94%E0%B8%AA%E0%B8%99%E0%B8%B1%E0%B8%9A%E0%B8%AA%E0%B8%99%E0%B8%B8%E0%B8%99%E0%B8%AA%E0%B8%96%E0%B8%B2%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88/MapServer/16/query?where=1%3D1&outFields=NAME,ADDRESS,TEL,NUM_BED&returnGeometry=false&f=json"

# ค้นหาโรงพยาบาลที่มีมากกว่า 100 เตียง
curl -s "https://bmagis.bangkok.go.th/arcgis/rest/services/%E0%B8%88%E0%B8%B8%E0%B8%94%E0%B8%AA%E0%B8%99%E0%B8%B1%E0%B8%9A%E0%B8%AA%E0%B8%99%E0%B8%B8%E0%B8%99%E0%B8%AA%E0%B8%96%E0%B8%B2%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88/MapServer/16/query?where=NUM_BED>100&outFields=NAME,NUM_BED,ADDRESS&returnGeometry=false&f=json"

# ดึงพร้อม geometry (GeoJSON)
curl -s "https://bmagis.bangkok.go.th/arcgis/rest/services/%E0%B8%88%E0%B8%B8%E0%B8%94%E0%B8%AA%E0%B8%99%E0%B8%B1%E0%B8%9A%E0%B8%AA%E0%B8%99%E0%B8%B8%E0%B8%99%E0%B8%AA%E0%B8%96%E0%B8%B2%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88/MapServer/16/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"
```

---

### Endpoint ๙: ศูนย์บริการสาธารณสุข

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนีภูมิภาสาธารณสุข/MapServer/96` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/96` |
| **URL ทางเลือก** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/ด้านสาธารณสุข/MapServer/3` (ชื่อ: พื้นที่ศูนย์บริการสาธารณสุข) |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### สิ่งที่ทดสอบแล้วกับ URL เดิม

| วิธีที่ทดสอบ | ผลลัพธ์ |
|-------------|--------|
| `?f=json` | HTTP 200 แต่ body คืน 404 error — "Service แผนีภูมิภาสาธารณสุข/MapServer not found" |
| ลองแก้ชื่อ service เป็น `แผนที่ภูมิภาสาธารณสุข` (แก้ "นี" เป็น "ที่") | ยังคง 404 — service ทั้งหมดถูกลบ |
| เปลี่ยนเป็น FeatureServer | 404 |
| สำรวจ service directory | พบ service ใหม่ `GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer` Layer 96 |
| ตรวจสอบ standalone service | พบ `ด้านสาธารณสุข/MapServer` Layer 3 (ข้อมูลเดียวกัน) |

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| NAME | String | ชื่อศูนย์บริการสาธารณสุข | 100 | ชื่อ |
| HEALTH_ID | String | รหัสศูนย์บริการสาธารณสุข | 3 | รหัส |
| ADDRESS | String | ที่อยู่ | 100 | ที่อยู่ |
| DCODE | String | รหัสเขต | 4 | รหัสเขต |
| MOU_POP | Integer | จำนวนประชากรรวม MOU สปสช | - | ประชากรที่รับผิดชอบ |
| MALE | Integer | จำนวนประชากรชาย ตาม MOU สปสช. | - | ประชากรชาย |
| FEMALE | Integer | จำนวนประชากรหญิง ตาม MOU สปสช. | - | ประชากรหญิง |
| HOUSE | Integer | จำนวนบ้านที่รับผิดชอบครอบครัว | - | จำนวนบ้าน |
| HOUSEHOLD | Integer | จำนวนบ้านที่รับผิดชอบหลังคาเรือน | - | หลังคาเรือน |
| EXT_WD | String | คลินิคนอกเวลา จ. - ศ. | 5 | บริการวันธรรมดา |
| EXT_WE | String | คลินิคนอกเวลา ส. - อา. | 5 | บริการวันหยุด |
| EXT_HEART | String | คลินิคนอกเวลาโรคหัวใจ | 5 | คลินิกหัวใจ |
| EXT_DB_HY | String | คลินิคนอกเวลาโรคเบาหวาน/ความดัน | 5 | คลินิกเบาหวาน |
| EXT_DERM | String | คลินิคนอกเวลาโรคผิวหนัง | 5 | คลินิกผิวหนัง |
| EXT_CHILD | String | คลินิคนอกเวลาโรคเด็ก | 5 | คลินิกเด็ก |
| EXT_EYE | String | คลินิคนอกเวลาโรคตา | 5 | คลินิกตา |
| EXT_ENT | String | คลินิคนอกเวลาหู คอ จมูก | 5 | คลินิก ENT |
| EXT_GYN | String | คลินิคนอกเวลาโรคสูตินรีเวช | 5 | คลินิกสูติฯ |
| EXT_ACP | String | คลินิคนอกเวลาฝังเข็ม | 5 | ฝังเข็ม |
| EXT_STD | String | คลินิคนอกเวลากามโรค | 5 | คลินิกกามโรค |
| EXT_ORT | String | คลินิคนอกเวลาโรคกระดูก | 5 | คลินิกกระดูก |
| EXT_TB | String | คลินิคนอกเวลาวัณโรค | 5 | คลินิกวัณโรค |
| EXT_PT | String | คลินิคนอกเวลากายภาพบำบัด | 5 | กายภาพบำบัด |
| TEL | String | หมายเลขโทรศัพท์ | 50 | เบอร์โทร |
| URL | String | ที่อยู่เว็บไซต์ | 100 | เว็บไซต์ |
| FACEBOOK | String | เฟซบุ๊ค | 100 | Facebook |
| TRANSPORT | String | การเดินทาง | 200 | วิธีเดินทาง |
| LAT | Double | ละติจูด | - | ละติจูด |
| LNG | Double | ลองจิจูด | - | ลองจิจูด |
| DNAME | String | สำนักงานเขต | 100 | ชื่อเขต |
| SHORT_NAME | String | ชื่อย่อ | 50 | ชื่อย่อ |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
# ดึงศูนย์บริการสาธารณสุขทั้งหมด
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/96/query?where=1%3D1&outFields=NAME,ADDRESS,TEL,DNAME,MOU_POP&returnGeometry=false&f=json"

# ดึงเป็น GeoJSON
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/96/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"
```

---

### Endpoint ๑๐: ศูนย์บริการสาธารณสุขสาขา

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนีภูมิภาสาธารณสุข/MapServer/97` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/97` |
| **URL ทางเลือก** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/ด้านสาธารณสุข/MapServer/4` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### สิ่งที่ทดสอบแล้วกับ URL เดิม

เหมือน Endpoint ๙ — Service `แผนีภูมิภาสาธารณสุข` ถูกลบทั้ง MapServer ไม่ว่าจะทดสอบด้วย `?f=json`, browser headers, FeatureServer, หรือแก้ชื่อสะกด ผลลัพธ์คือ 404 ทุกกรณี

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| NAME | String | ชื่อศูนย์บริการสาธารณสุขสาขา | 100 | ชื่อ |
| ADDRESS | String | ที่อยู่ | 200 | ที่อยู่ |
| DCODE | String | รหัสเขต | 4 | รหัสเขต |
| TEL | String | หมายเลขโทรศัพท์ | 50 | เบอร์โทร |
| LAT | Double | ละติจูด | - | ละติจูด |
| LNG | Double | ลองจิจูด | - | ลองจิจูด |
| DNAME | String | สำนักงานเขต | 100 | ชื่อเขต |
| BRANCH_ID | String | รหัสศูนย์บริการสาธารณสุขสาขา | 4 | รหัสสาขา |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
# ดึงศูนย์บริการสาธารณสุขสาขาทั้งหมด
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/97/query?where=1%3D1&outFields=NAME,ADDRESS,TEL,DNAME&returnGeometry=false&f=json"
```

---

### Endpoint ๑๑: โรงควบคุมคุณภาพน้ำและบำบัดน้ำเสีย

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนีภูมิภาสาธารณูปโภค/MapServer/73` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/73` |
| **URL ทางเลือก** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/WQM/MapServer/0` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### สิ่งที่ทดสอบแล้วกับ URL เดิม

| วิธีที่ทดสอบ | ผลลัพธ์ |
|-------------|--------|
| `?f=json` ต่อท้าย URL เดิม | HTTP 200 แต่ body คืน 404 — "Service แผนีภูมิภาสาธารณูปโภค/MapServer not found" |
| เปลี่ยนเป็น FeatureServer | 404 |
| สำรวจ service directory | Service เดิมถูกลบ พบ service ใหม่ `แผนที่ภูมิศาสตร์ตามด้าน/MapServer` Layer 73 |
| ตรวจสอบ standalone service | พบ `WQM/MapServer/0` (ข้อมูลคล้ายกัน) |

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | FID | - | |
| ID | Integer | ID | - | รหัส |
| NAME | String | NAME | 100 | ชื่อโรงควบคุมฯ |
| DCODE | String | DCODE | 4 | รหัสเขต |
| VOLUME | String | VOLUME | 35 | ปริมาณ |
| CPUDGITHEMATIC.WQM.AREA | String | AREA | 35 | พื้นที่ |
| LENGTH | String | LENGTH | 35 | ความยาว |
| SERVICE | String | SERVICE | 150 | พื้นที่ให้บริการ |
| LOCATION | String | LOCATION | 75 | ที่ตั้ง |
| SEWAGE | String | SEWAGE | 50 | ระบบบำบัด |
| X | Double | X | - | พิกัด X |
| Y | Double | Y | - | พิกัด Y |
| URL | String | URL | 200 | URL |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
# ดึงข้อมูลโรงควบคุมคุณภาพน้ำทั้งหมด
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/73/query?where=1%3D1&outFields=NAME,SERVICE,LOCATION,SEWAGE&returnGeometry=false&f=json"
```

---

### Endpoint ๑๒: ศูนย์กำจัดขยะ

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนีภูมิภาสาธารณูปโภค/MapServer/70` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/70` |
| **URL ทางเลือก** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/WASTE_CENTER/MapServer/0` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |

#### สิ่งที่ทดสอบแล้วกับ URL เดิม

เหมือน Endpoint ๑๑ — Service `แผนีภูมิภาสาธารณูปโภค` ถูกลบทั้ง MapServer ทดสอบด้วย `?f=json` และ FeatureServer แล้วคืน 404

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | FID | - | |
| NAME | String | NAME | 30 | ชื่อศูนย์กำจัดขยะ |
| DCODE | String | DCODE | 4 | รหัสเขต |
| ADDRESS | String | ADDRESS | 70 | ที่อยู่ |
| QUANTITY | Integer | QUANTITY | - | ปริมาณ |
| X | Double | X | - | พิกัด X |
| Y | Double | Y | - | พิกัด Y |
| URL | String | URL | 200 | URL |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
# ดึงข้อมูลศูนย์กำจัดขยะทั้งหมด
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/70/query?where=1%3D1&outFields=NAME,ADDRESS,QUANTITY&returnGeometry=false&f=json"
```

---

### Endpoint ๑๓: โรงเรียนสังกัด กทม

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนีภูมิภาการศึกษา/MapServer/87` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/87` |
| **URL ทางเลือก** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/ด้านการศึกษา/MapServer/7` หรือ `BMA_SCHOOL/MapServer/0` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |

#### สิ่งที่ทดสอบแล้วกับ URL เดิม (ใช้ได้กับ Endpoint ๑๓-๑๗ ทั้งหมด)

| วิธีที่ทดสอบ | ผลลัพธ์ |
|-------------|--------|
| `?f=json` ต่อท้าย URL เดิม | HTTP 200 แต่ body คืน 404 — "Service แผนีภูมิภาการศึกษา/MapServer not found" |
| MapServer root (ไม่ใส่ layer ID) | 404 — ทั้ง MapServer ถูกลบ |
| เปลี่ยนเป็น FeatureServer | 404 |
| สำรวจ service directory | พบ service ใหม่ `แผนที่ภูมิศาสตร์ตามด้าน/MapServer` ที่มี Layer 86-91 |
| ตรวจสอบ standalone service | พบ `ด้านการศึกษา/MapServer` (Layer 0-7) และ `BMA_SCHOOL/MapServer/0` |

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | FID | - | |
| TYPE | String | TYPE | 1 | ประเภท |
| NAME | String | NAME | 150 | ชื่อโรงเรียน |
| DCODE | String | DCODE | 4 | รหัสเขต |
| ADDRESS | String | ADDRESS | 100 | ที่อยู่ |
| X | Double | X | - | พิกัด X |
| Y | Double | Y | - | พิกัด Y |
| URL | String | URL | 200 | URL |
| SHAPE | Geometry | SHAPE | - | |
| DISTRICT_ID | String | DISTRICT_ID | 10 | รหัสเขต |
| DISTRIC_NAME | String | DISTRIC_NAME | 70 | ชื่อเขต |
| SUBDISTR_ID | String | SUBDISTR_ID | 10 | รหัสแขวง |
| SUBDISTR_NAME | String | SUBDISTR_NAME | 70 | ชื่อแขวง |
| SCH_LEVEL | String | SCH_LEVEL | 4 | ระดับชั้น |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
# ดึงโรงเรียนสังกัด กทม ทั้งหมด
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/87/query?where=1%3D1&outFields=NAME,ADDRESS,DISTRIC_NAME,SCH_LEVEL&returnGeometry=false&f=json"

# นับจำนวนโรงเรียน
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/87/query?where=1%3D1&returnCountOnly=true&f=json"
```

---

### Endpoint ๑๔: โรงเรียนเอกชน

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนีภูมิภาการศึกษา/MapServer/88` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/88` |
| **URL ทางเลือก** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/ด้านการศึกษา/MapServer/1` หรือ `PRIVATE_SCHOOL/MapServer/0` |
| **Type** | Feature Layer (Point) |

#### สิ่งที่ทดสอบแล้ว

เหมือน Endpoint ๑๓ — Service `แผนีภูมิภาการศึกษา` ถูกลบทั้ง MapServer

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length |
|-----------|------|-------|--------|
| OBJECTID | OID | FID | - |
| NAME | String | NAME | 100 |
| DCODE | String | DCODE | 6 |
| NUM_STU | Integer | NUM_STU | - |
| ADDRESS | String | ADDRESS | 150 |
| X | Double | X | - |
| Y | Double | Y | - |
| URL | Double | URL | - |
| SHAPE | Geometry | SHAPE | - |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/88/query?where=1%3D1&outFields=NAME,ADDRESS,NUM_STU&returnGeometry=false&f=json"
```

---

### Endpoint ๑๕: โรงเรียนสังกัด สพฐ

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนีภูมิภาการศึกษา/MapServer/86` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/86` |
| **URL ทางเลือก** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/ด้านการศึกษา/MapServer/6` หรือ `BEC_SCHOOL/MapServer/0` |
| **Type** | Feature Layer (Point) |

#### สิ่งที่ทดสอบแล้ว

เหมือน Endpoint ๑๓

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length |
|-----------|------|-------|--------|
| OBJECTID | OID | FID | - |
| NAME | String | NAME | 45 |
| DCODE | String | DCODE | 13 |
| ADDRESS | String | ADDRESS | 150 |
| NUM_STU | Integer | NUM_STU | - |
| X | Double | X | - |
| Y | Double | Y | - |
| URL | String | URL | 200 |
| SHAPE | Geometry | SHAPE | - |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/86/query?where=1%3D1&outFields=NAME,ADDRESS,NUM_STU&returnGeometry=false&f=json"
```

---

### Endpoint ๑๖: มหาวิทยาลัย

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนีภูมิภาการศึกษา/MapServer/89` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/89` |
| **URL ทางเลือก** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/ด้านการศึกษา/MapServer/0` |
| **Type** | Feature Layer (Point) |

#### สิ่งที่ทดสอบแล้ว

เหมือน Endpoint ๑๓

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length |
|-----------|------|-------|--------|
| OBJECTID | OID | FID | - |
| NAME | String | NAME | 100 |
| DCODE | String | DCODE | 4 |
| TYPE | String | TYPE | 1 |
| ADDRESS | String | ADDRESS | 120 |
| X | Double | X | - |
| Y | Double | Y | - |
| URL | String | URL | 200 |
| SHAPE | Geometry | SHAPE | - |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/89/query?where=1%3D1&outFields=NAME,TYPE,ADDRESS&returnGeometry=false&f=json"
```

---

### Endpoint ๑๗: วิทยาลัย

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนีภูมิภาการศึกษา/MapServer/91` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/91` |
| **URL ทางเลือก** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/ด้านการศึกษา/MapServer/4` |
| **Type** | Feature Layer (Point) |

#### สิ่งที่ทดสอบแล้ว

เหมือน Endpoint ๑๓

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length |
|-----------|------|-------|--------|
| OBJECTID | OID | FID | - |
| ID | Integer | ID | - |
| NAME | String | NAME | 120 |
| DCODE | String | DCODE | 4 |
| ADDRESS | String | ADDRESS | 120 |
| X | Double | X | - |
| Y | Double | Y | - |
| URL | String | URL | 200 |
| SHAPE | Geometry | SHAPE | - |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/91/query?where=1%3D1&outFields=NAME,ADDRESS&returnGeometry=false&f=json"
```

---

### Endpoint ๑๘: ตลาดที่ขึ้นทะเบียนกับกทม.

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | **URL เดิมใช้ไม่ได้ — พบ URL ใหม่แล้ว** |
| **URL เดิม (ใช้ไม่ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนีภูมิภาคาณิชยกรรม/MapServer/82` |
| **URL ใหม่ (ใช้ได้)** | `https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/82` |
| **ArcGIS Version** | 11.3 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 32647 (UTM Zone 47N) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |

#### สิ่งที่ทดสอบแล้วกับ URL เดิม

| วิธีที่ทดสอบ | ผลลัพธ์ |
|-------------|--------|
| `?f=json` ต่อท้าย URL เดิม | HTTP 200 แต่ body คืน 404 — "Service แผนีภูมิภาคาณิชยกรรม/MapServer not found" |
| MapServer root | 404 — ทั้ง MapServer ถูกลบ |
| เปลี่ยนเป็น FeatureServer | 404 |
| สำรวจ service directory | พบ service ใหม่ `แผนที่ภูมิศาสตร์ตามด้าน/MapServer` Layer 82 |

#### Fields (จาก URL ใหม่)

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | FID | - | |
| DCODE | String | DCODE | 4 | รหัสเขต |
| MAR_NAME | String | MAR_NAME | 70 | ชื่อตลาด |
| ADDRESS | String | ADDRESS | 100 | ที่อยู่ |
| MANAGE | String | MANAGE | 2 | ผู้จัดการ |
| X | Double | X | - | พิกัด X |
| Y | Double | Y | - | พิกัด Y |
| URL | String | URL | 200 | URL |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query (ใช้ URL ใหม่)

```bash
# ดึงข้อมูลตลาดทั้งหมด
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/82/query?where=1%3D1&outFields=MAR_NAME,ADDRESS,DCODE&returnGeometry=false&f=json"

# ดึงเป็น GeoJSON
curl -s "https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/82/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"
```

---

### Endpoint ๑๙: ข้อมูลมลพิษทางอากาศ (PM2.5)

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmagis.bangkok.go.th/arcgis/rest/services/Hosted/air_quality_data_processed/FeatureServer/0` |
| **ArcGIS Version** | 10.71 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 102100 (Web Mercator) |
| **Capabilities** | Query |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| objectid | OID | OBJECTID | - | |
| district | String | district | 8000 | เขต/สถานี |
| pm10 | Double | PM10 | - | ค่า PM10 (μg/m³) |
| pm2_5 | Double | PM2.5 | - | ค่า PM2.5 (μg/m³) |
| co | Double | CO | - | Carbon Monoxide |
| no2 | Double | NO2 | - | Nitrogen Dioxide |
| ws | Double | WS | - | Wind Speed |
| wd | Double | WD | - | Wind Direction |
| temp | Double | Temp | - | Temperature |
| rh | Double | RH | - | Relative Humidity |
| bp | Double | BP | - | Barometric Pressure |
| o3 | Double | O3 | - | Ozone |
| rain | Double | RAIN | - | ปริมาณฝน |
| latitude | Double | Latitude | - | ละติจูด |
| longitude | Double | Longitude | - | ลองจิจูด |
| date_time | Date | date_time | - | วันเวลาที่วัด |

#### ตัวอย่างการ Query

```bash
# ดึงข้อมูล PM2.5 ล่าสุดทั้งหมด
curl -s "https://bmagis.bangkok.go.th/arcgis/rest/services/Hosted/air_quality_data_processed/FeatureServer/0/query?where=1%3D1&outFields=district,pm2_5,pm10,date_time&returnGeometry=false&f=json&resultRecordCount=50"

# ค้นหาสถานีที่ PM2.5 เกินมาตรฐาน (>50 μg/m³)
curl -s "https://bmagis.bangkok.go.th/arcgis/rest/services/Hosted/air_quality_data_processed/FeatureServer/0/query?where=pm2_5>50&outFields=district,pm2_5,date_time&returnGeometry=false&f=json"

# ดึงพร้อมพิกัด (GeoJSON, แปลงเป็น WGS84)
curl -s "https://bmagis.bangkok.go.th/arcgis/rest/services/Hosted/air_quality_data_processed/FeatureServer/0/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"

# ค่าเฉลี่ย PM2.5 ทั้งหมด
curl -s "https://bmagis.bangkok.go.th/arcgis/rest/services/Hosted/air_quality_data_processed/FeatureServer/0/query?where=1%3D1&outStatistics=%5B%7B%22statisticType%22%3A%22avg%22%2C%22onStatisticField%22%3A%22pm2_5%22%2C%22outStatisticFieldName%22%3A%22avg_pm25%22%7D%5D&f=json"
```

---

### Endpoint ๒๐: กิจการแพลนท์ปูน

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/0` |
| **ArcGIS Version** | 11.5 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 4326 (WGS 84) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| CEMENT_PLANT_ID | Double | CEMENT_PLANT_ID | - | รหัสแพลนท์ปูน |
| NAME | String | NAME | 255 | ชื่อสถานประกอบการ |
| ADDRESS | String | ADDRESS | 255 | ที่อยู่ |
| HOUSENO | String | HOUSENO | 255 | เลขที่ |
| MOO | String | MOO | 255 | หมู่ |
| SOI | String | SOI | 255 | ซอย |
| ROAD | String | ROAD | 255 | ถนน |
| SCODE | Double | SCODE | - | รหัสแขวง |
| SNAME | String | SNAME | 255 | แขวง |
| DCODE | Double | DCODE | - | รหัสเขต |
| DNAME | String | DNAME | 255 | เขต |
| LATITUDE | Double | LATITUDE | - | ละติจูด |
| LONGITUDE | Double | LONGITUDE | - | ลองจิจูด |
| HYGIENIC | String | HYGIENIC | 255 | ใบอนุญาตสุขลักษณะ |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query

```bash
# ดึงกิจการแพลนท์ปูนทั้งหมด
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/0/query?where=1%3D1&outFields=NAME,ADDRESS,DNAME,SNAME,HYGIENIC&returnGeometry=false&f=json"

# ค้นหาตามเขต
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/0/query?where=DNAME%3D'บางขุนเทียน'&outFields=*&f=json"

# นับจำนวน
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/0/query?where=1%3D1&returnCountOnly=true&f=json"

# ดึงเป็น GeoJSON
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/0/query?where=1%3D1&outFields=*&f=geojson"
```

---

### Endpoint ๒๑: โครงการก่อสร้าง 50 เขต

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/1` |
| **ArcGIS Version** | 11.5 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 4326 (WGS 84) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| ID | Double | ID | - | รหัส |
| NAME | String | NAME | 255 | ชื่อโครงการ |
| LATITUDE | Double | LATITUDE | - | ละติจูด |
| LONGITUDE | Double | LONGITUDE | - | ลองจิจูด |
| BEGIN_POINT | String | BEGIN_POINT | 255 | จุดเริ่มต้น |
| STOP_POINT | String | STOP_POINT | 255 | จุดสิ้นสุด |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query

```bash
# ดึงโครงการก่อสร้างทั้งหมด
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/1/query?where=1%3D1&outFields=NAME,BEGIN_POINT,STOP_POINT,LATITUDE,LONGITUDE&returnGeometry=false&f=json"

# ค้นหาโครงการตามชื่อ
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/1/query?where=NAME+LIKE+'%25สะพาน%25'&outFields=*&f=json"
```

---

### Endpoint ๒๒: โรงงานที่รายงาน รว.3

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/2` |
| **ArcGIS Version** | 11.5 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 4326 (WGS 84) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| FACTORY_CODE | Double | FACTORY_CODE | - | รหัสโรงงาน |
| NAME | String | NAME | 100 | ชื่อโรงงาน |
| HOUSENO | String | HOUSENO | 30 | เลขที่ |
| MOO | String | MOO | 10 | หมู่ |
| SOI | String | SOI | 30 | ซอย |
| SNAME | String | SNAME | 30 | แขวง |
| DNAME | String | DNAME | 30 | เขต |
| LATITUDE | Double | LATITUDE | - | ละติจูด |
| LONGITUDE | Double | LONGITUDE | - | ลองจิจูด |
| CAL_DUST | Double | CAL_DUST | - | ค่าฝุ่น (คำนวณ) |
| CAL_SO2 | Double | CAL_SO2 | - | ค่า SO2 (คำนวณ) |
| CAL_NOX_NO2 | Double | CAL_NOX_NO2 | - | ค่า NOx/NO2 (คำนวณ) |
| SCODE | String | SCODE | 6 | รหัสแขวง |
| DCODE | String | DCODE | 4 | รหัสเขต |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query

```bash
# ดึงโรงงานทั้งหมด
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/2/query?where=1%3D1&outFields=NAME,DNAME,SNAME,CAL_DUST,CAL_SO2,CAL_NOX_NO2&returnGeometry=false&f=json"

# โรงงานที่มีค่าฝุ่นสูง
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/2/query?where=CAL_DUST>0&outFields=NAME,DNAME,CAL_DUST&returnGeometry=false&f=json&orderByFields=CAL_DUST+DESC"

# นับจำนวนโรงงานต่อเขต
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/2/query?where=1%3D1&groupByFieldsForStatistics=DNAME&outStatistics=%5B%7B%22statisticType%22%3A%22count%22%2C%22onStatisticField%22%3A%22OBJECTID%22%2C%22outStatisticFieldName%22%3A%22factory_count%22%7D%5D&f=json"
```

---

### Endpoint ๒๓: กิจการผลิต สะสม แบ่งบรรจุธูป

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/3` |
| **ArcGIS Version** | 11.5 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 4326 (WGS 84) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| INCENSE_ID | Double | INCENSE_ID | - | รหัสกิจการ |
| ADDRESS | String | ADDRESS | 255 | ที่อยู่ |
| HOUSENO | String | HOUSENO | 255 | เลขที่ |
| MOO | String | MOO | 255 | หมู่ |
| SOI | String | SOI | 255 | ซอย |
| ROAD | String | ROAD | 255 | ถนน |
| SCODE | Double | SCODE | - | รหัสแขวง |
| SNAME | String | SNAME | 255 | แขวง |
| DCODE | Double | DCODE | - | รหัสเขต |
| DNAME | String | DNAME | 255 | เขต |
| LATITUDE | Double | LATITUDE | - | ละติจูด |
| LONGITUDE | Double | LONGITUDE | - | ลองจิจูด |
| HYGIENIC | String | HYGIENIC | 255 | ใบอนุญาตสุขลักษณะ |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query

```bash
# ดึงข้อมูลทั้งหมด
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/3/query?where=1%3D1&outFields=ADDRESS,DNAME,SNAME,HYGIENIC&returnGeometry=false&f=json"

# ค้นหาตามเขต
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/3/query?where=DNAME%3D'ทวีวัฒนา'&outFields=*&f=json"
```

---

### Endpoint ๒๔: กิจการอู่พ่นสีรถยนต์

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/4` |
| **ArcGIS Version** | 11.5 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 4326 (WGS 84) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| KNOCKING_ID | Double | KNOCKING_ID | - | รหัสอู่ |
| ADDRESS | String | ADDRESS | 255 | ที่อยู่ |
| HOUSENO | String | HOUSENO | 255 | เลขที่ |
| MOO | String | MOO | 255 | หมู่ |
| SOI | String | SOI | 255 | ซอย |
| ROAD | String | ROAD | 255 | ถนน |
| SCODE | Double | SCODE | - | รหัสแขวง |
| SNAME | String | SNAME | 255 | แขวง |
| DCODE | Double | DCODE | - | รหัสเขต |
| DNAME | String | DNAME | 255 | เขต |
| LATITUDE | Double | LATITUDE | - | ละติจูด |
| LONGITUDE | Double | LONGITUDE | - | ลองจิจูด |
| HYGIENIC | String | HYGIENIC | 255 | ใบอนุญาตสุขลักษณะ |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query

```bash
# ดึงข้อมูลทั้งหมด
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/4/query?where=1%3D1&outFields=ADDRESS,DNAME,SNAME,ROAD,HYGIENIC&returnGeometry=false&f=json"

# ดึงเป็น GeoJSON
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/4/query?where=1%3D1&outFields=*&f=geojson"
```

---

### Endpoint ๒๕: จุดตรวจวัดควันดำ

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/6` |
| **ArcGIS Version** | 11.5 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 4326 (WGS 84) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| ID | Double | ID | - | รหัส |
| NAME | String | NAME | 255 | ชื่อจุดตรวจ |
| DNAME | String | DNAME | 255 | เขต |
| DCODE | Double | DCODE | - | รหัสเขต |
| LATITUDE | Double | LATITUDE | - | ละติจูด |
| LONGITUDE | Double | LONGITUDE | - | ลองจิจูด |
| VEHICLE_TYPE | String | VEHICLE_TYPE | 255 | ประเภทรถที่ตรวจ |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query

```bash
# ดึงจุดตรวจทั้งหมด
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/6/query?where=1%3D1&outFields=NAME,DNAME,VEHICLE_TYPE,LATITUDE,LONGITUDE&returnGeometry=false&f=json"

# ค้นหาตามเขต
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/6/query?where=DNAME%3D'ดินแดง'&outFields=*&f=json"
```

---

### Endpoint ๒๖: กิจการประดิษฐ์หินเป็นสิ่งของเครื่องใช้

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/7` |
| **ArcGIS Version** | 11.5 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 4326 (WGS 84) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| STONE_ID | Double | STONE_ID | - | รหัสกิจการ |
| ADDRESS | String | ADDRESS | 255 | ที่อยู่ |
| HOUSENO | String | HOUSENO | 255 | เลขที่ |
| MOO | String | MOO | 255 | หมู่ |
| SOI | String | SOI | 255 | ซอย |
| ROAD | String | ROAD | 255 | ถนน |
| SCODE | Double | SCODE | - | รหัสแขวง |
| SNAME | String | SNAME | 255 | แขวง |
| DCODE | Double | DCODE | - | รหัสเขต |
| DNAME | String | DNAME | 255 | เขต |
| LATITUDE | Double | LATITUDE | - | ละติจูด |
| LONGITUDE | Double | LONGITUDE | - | ลองจิจูด |
| HYGIENIC | String | HYGIENIC | 255 | ใบอนุญาตสุขลักษณะ |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query

```bash
# ดึงข้อมูลทั้งหมด
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/7/query?where=1%3D1&outFields=ADDRESS,DNAME,SNAME,HYGIENIC&returnGeometry=false&f=json"
```

---

### Endpoint ๒๗: กิจการที่มีการใช้หม้อน้ำ

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/8` |
| **ArcGIS Version** | 11.5 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 4326 (WGS 84) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| BOILER_ID | Double | BOILER_ID | - | รหัสกิจการ |
| ADDRESS | String | ADDRESS | 255 | ที่อยู่ |
| HOUSENO | String | HOUSENO | 255 | เลขที่ |
| SOI | String | SOI | 255 | ซอย |
| ROAD | String | ROAD | 255 | ถนน |
| SNAME | String | SNAME | 255 | แขวง |
| DNAME | String | DNAME | 255 | เขต |
| LAT | Double | LAT | - | ละติจูด |
| LONG_ | Double | LONG_ | - | ลองจิจูด |
| HYGIENIC | String | HYGIENIC | 255 | ใบอนุญาตสุขลักษณะ |
| SHAPE | Geometry | SHAPE | - | |

> **หมายเหตุ:** Layer นี้ใช้ field ชื่อ `LAT`/`LONG_` แทน `LATITUDE`/`LONGITUDE` และไม่มี field `MOO`, `SCODE`, `DCODE`

#### ตัวอย่างการ Query

```bash
# ดึงข้อมูลทั้งหมด
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/8/query?where=1%3D1&outFields=ADDRESS,DNAME,SNAME,ROAD,HYGIENIC&returnGeometry=false&f=json"
```

---

### Endpoint ๒๘: กิจการหลอมหรือหล่อโลหะ

| รายละเอียด | ค่า |
|-----------|-----|
| **สถานะ** | ใช้งานได้ |
| **Layer URL** | `https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/9` |
| **ArcGIS Version** | 11.5 |
| **Type** | Feature Layer |
| **Geometry** | esriGeometryPoint |
| **Spatial Reference** | WKID 4326 (WGS 84) |
| **Capabilities** | Map, Query, Data |
| **Max Record Count** | 2,000 |
| **Supported Formats** | JSON, geoJSON, PBF |

#### Fields

| Field Name | Type | Alias | Length | คำอธิบาย |
|-----------|------|-------|--------|---------|
| OBJECTID | OID | OBJECTID | - | |
| VULCANIZATION_ID | Double | VULCANIZATION_ID | - | รหัสกิจการ |
| ADDRESS | String | ADDRESS | 255 | ที่อยู่ |
| HOUSENO | String | HOUSENO | 255 | เลขที่ |
| MOO | String | MOO | 255 | หมู่ |
| SOI | String | SOI | 255 | ซอย |
| ROAD | String | ROAD | 255 | ถนน |
| SCODE | Double | SCODE | - | รหัสแขวง |
| SNAME | String | SNAME | 255 | แขวง |
| DCODE | Double | DCODE | - | รหัสเขต |
| DNAME | String | DNAME | 255 | เขต |
| LATITUDE | Double | LATITUDE | - | ละติจูด |
| LONGITUDE | Double | LONGITUDE | - | ลองจิจูด |
| HYGIENIC | String | HYGIENIC | 255 | ใบอนุญาตสุขลักษณะ |
| SHAPE | Geometry | SHAPE | - | |

#### ตัวอย่างการ Query

```bash
# ดึงข้อมูลทั้งหมด
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/9/query?where=1%3D1&outFields=ADDRESS,DNAME,SNAME,ROAD,HYGIENIC&returnGeometry=false&f=json"

# ดึงเป็น GeoJSON
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/9/query?where=1%3D1&outFields=*&f=geojson"
```

---

## 5. ตัวอย่างการใช้งานขั้นสูง

### 5.1 ดึงข้อมูลทั้งหมดด้วย Pagination (Python)

เนื่องจากแต่ละ Layer มี `maxRecordCount` จำกัด (1,000-2,000) การดึงข้อมูลทั้งหมดต้องใช้ pagination:

```python
import requests

def fetch_all_features(base_url, where="1=1", out_fields="*", out_sr=4326):
    """ดึง features ทั้งหมดจาก ArcGIS REST API ด้วย pagination"""
    all_features = []
    offset = 0
    batch_size = 1000

    while True:
        params = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": out_sr,
            "f": "json",
            "resultRecordCount": batch_size,
            "resultOffset": offset
        }
        response = requests.get(f"{base_url}/query", params=params)
        data = response.json()

        features = data.get("features", [])
        if not features:
            break

        all_features.extend(features)
        print(f"  Fetched {len(all_features)} features...")

        if len(features) < batch_size:
            break
        offset += batch_size

    return all_features

# ตัวอย่าง: ดึงข้อมูลเขตการปกครองทั้งหมด
url = "https://cpudgiapp.bangkok.go.th/arcgis/rest/services/Basemap_Service/CPUD_Basemap1000/MapServer/12"
features = fetch_all_features(url)
print(f"Total features: {len(features)}")
```

### 5.2 แปลง ArcGIS Response เป็น GeoDataFrame (GeoPandas)

```python
import requests
import geopandas as gpd
from shapely.geometry import shape

def arcgis_to_geodataframe(base_url, where="1=1", out_sr=4326):
    """ดึงข้อมูลจาก ArcGIS REST API แล้วแปลงเป็น GeoDataFrame"""
    params = {
        "where": where,
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": out_sr,
        "f": "geojson"
    }
    response = requests.get(f"{base_url}/query", params=params)
    geojson = response.json()

    gdf = gpd.GeoDataFrame.from_features(geojson["features"])
    gdf.set_crs(epsg=out_sr, inplace=True)
    return gdf

# ตัวอย่าง: โหลดข้อมูลโรงพยาบาล
hospital_url = "https://bmagis.bangkok.go.th/arcgis/rest/services/%E0%B8%88%E0%B8%B8%E0%B8%94%E0%B8%AA%E0%B8%99%E0%B8%B1%E0%B8%9A%E0%B8%AA%E0%B8%99%E0%B8%B8%E0%B8%99%E0%B8%AA%E0%B8%96%E0%B8%B2%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88/MapServer/16"
gdf = arcgis_to_geodataframe(hospital_url)
print(gdf.head())
```

### 5.3 Spatial Query: ค้นหาสถานที่ภายในรัศมี

```bash
# ค้นหาโรงงานภายในรัศมี 5 กม. จากจุด (lon=100.5, lat=13.75)
# ใช้ buffer parameter (distance in meters, ต้องแปลง geometry เป็น Web Mercator)
curl -s "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/2/query?geometry=100.5,13.75&geometryType=esriGeometryPoint&inSR=4326&spatialRel=esriSpatialRelIntersects&distance=5000&units=esriSRUnit_Meter&outFields=NAME,DNAME,CAL_DUST&returnGeometry=false&f=json"
```

### 5.4 รวมข้อมูลมลพิษทั้งหมดจาก dust_pollution MapServer

```python
import requests

dust_layers = {
    0: "กิจการแพลนท์ปูน",
    1: "โครงการก่อสร้าง 50 เขต",
    2: "โรงงานที่รายงาน รว.3",
    3: "กิจการผลิต สะสม แบ่งบรรจุธูป",
    4: "กิจการอู่พ่นสีรถยนต์",
    6: "จุดตรวจวัดควันดำ",
    7: "กิจการประดิษฐ์หินเป็นสิ่งของเครื่องใช้",
    8: "กิจการที่มีการใช้หม้อน้ำ",
    9: "กิจการหลอมหรือหล่อโลหะ",
}

base = "https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer"

for layer_id, name in dust_layers.items():
    params = {"where": "1=1", "returnCountOnly": "true", "f": "json"}
    resp = requests.get(f"{base}/{layer_id}/query", params=params)
    count = resp.json().get("count", "error")
    print(f"Layer {layer_id}: {name} — {count} records")
```

### 5.5 ใช้กับ Leaflet.js (Web Map)

```html
<!DOCTYPE html>
<html>
<head>
  <link rel="stylesheet" href="https://unpkg.com/leaflet/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet/dist/leaflet.js"></script>
  <script src="https://unpkg.com/esri-leaflet/dist/esri-leaflet.js"></script>
  <style>#map { height: 100vh; }</style>
</head>
<body>
  <div id="map"></div>
  <script>
    const map = L.map('map').setView([13.75, 100.5], 11);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);

    // เพิ่ม layer โรงพยาบาล
    L.esri.featureLayer({
      url: 'https://bmagis.bangkok.go.th/arcgis/rest/services/%E0%B8%88%E0%B8%B8%E0%B8%94%E0%B8%AA%E0%B8%99%E0%B8%B1%E0%B8%9A%E0%B8%AA%E0%B8%99%E0%B8%B8%E0%B8%99%E0%B8%AA%E0%B8%96%E0%B8%B2%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88/MapServer/16',
      pointToLayer: (feature, latlng) => L.circleMarker(latlng, {radius: 6, color: 'red'}),
      onEachFeature: (feature, layer) => {
        layer.bindPopup(`<b>${feature.properties.NAME}</b><br>${feature.properties.ADDRESS}`);
      }
    }).addTo(map);

    // เพิ่ม layer กิจการแพลนท์ปูน
    L.esri.featureLayer({
      url: 'https://bmamap.bangkok.go.th/bmamap/rest/services/HEALTHMAP/dust_pollution/MapServer/0',
      pointToLayer: (feature, latlng) => L.circleMarker(latlng, {radius: 5, color: 'orange'}),
      onEachFeature: (feature, layer) => {
        layer.bindPopup(`<b>${feature.properties.NAME || 'แพลนท์ปูน'}</b><br>${feature.properties.ADDRESS || ''}`);
      }
    }).addTo(map);
  </script>
</body>
</html>
```

---

## 6. หมายเหตุและข้อจำกัด

### 6.1 Endpoints ที่ URL เดิมใช้ไม่ได้ — แต่พบ URL ใหม่แล้ว (11 รายการ)

ทั้ง 11 endpoint ที่อยู่บน server `cpudgiportal.bangkok.go.th` — URL เดิมคืนค่า 404 Service Not Found เนื่องจาก service เดิมถูก**ลบออก**และรวมเข้ากับ service ใหม่

#### สิ่งที่ทดสอบแล้ว (ครบทุก endpoint)

1. **เรียก URL เดิมด้วย `?f=json`** — HTTP 200 แต่ body คืน `{"error":{"code":404,"message":"Service ... not found"}}`
2. **เรียก URL เดิมด้วย `?f=pjson`** — ผลลัพธ์เหมือนกัน
3. **เรียก URL เดิมแบบ HTML (ไม่ใส่ `f`)** — HTTP 404
4. **เพิ่ม browser User-Agent + Referer header** — ไม่ช่วย ยังคืน 404 error ใน JSON body
5. **เรียก MapServer root (ไม่ใส่ layer ID)** — 404 ทั้ง MapServer ถูกลบ
6. **เรียก `/query` endpoint โดยตรง** — 404
7. **เปลี่ยนจาก MapServer เป็น FeatureServer** — 404
8. **ลองแก้ชื่อ service** (เช่น "แผนีภูมิภา" → "แผนที่ภูมิภา") — ยังคง 404 ไม่ว่าจะสะกดอย่างไร
9. **สำรวจ `GI_Platform?f=json` (service directory)** — พบว่า folder มีเหลือแค่ 2 services (`กฎหมายผังเมือง` และ `แผนที่ภูมิศาสตร์ตามด้าน`) — service เดิมทั้ง 5 ตัวถูกลบไปแล้ว
10. **สำรวจ root services directory** — **พบ service ใหม่** `GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer` ที่รวม layer ทั้งหมด 114 layers (รวม Layer ID เดิมครบทั้ง 11 รายการ)
11. **ทดสอบ Layer ID เดิมบน service ใหม่** — ทุก layer (43, 70, 73, 82, 86, 87, 88, 89, 91, 96, 97) **ใช้งานได้ปกติ** พร้อม query ข้อมูล
12. **พบ standalone services ทางเลือก** — เช่น `BMA_SCHOOL/MapServer/0`, `PRIVATE_SCHOOL/MapServer/0`, `WASTE_CENTER/MapServer/0`, `ด้านสาธารณสุข/MapServer`, `ด้านการศึกษา/MapServer`

#### สรุปการย้าย

| กลุ่ม Service เดิม (ถูกลบ) | Endpoints | URL ใหม่ (ใช้ได้) |
|---------------------------|-----------|-------------------|
| แผนที่ภูมิภาคารคมนาคมขนส่ง | ๔ | `GI_Platform/แผนที่ภูมิศาสตร์ตามด้าน/MapServer/43` |
| แผนีภูมิภาสาธารณสุข | ๙, ๑๐ | `.../MapServer/96`, `.../MapServer/97` |
| แผนีภูมิภาสาธารณูปโภค | ๑๑, ๑๒ | `.../MapServer/73`, `.../MapServer/70` |
| แผนีภูมิภาการศึกษา | ๑๓-๑๗ | `.../MapServer/87`, `/88`, `/86`, `/89`, `/91` |
| แผนีภูมิภาคาณิชยกรรม | ๑๘ | `.../MapServer/82` |

**URL ใหม่แบบเต็ม (encoded):**
```
https://cpudgiportal.bangkok.go.th/arcgis/rest/services/GI_Platform/%E0%B9%81%E0%B8%9C%E0%B8%99%E0%B8%97%E0%B8%B5%E0%B9%88%E0%B8%A0%E0%B8%B9%E0%B8%A1%E0%B8%B4%E0%B8%A8%E0%B8%B2%E0%B8%AA%E0%B8%95%E0%B8%A3%E0%B9%8C%E0%B8%95%E0%B8%B2%E0%B8%A1%E0%B8%94%E0%B9%89%E0%B8%B2%E0%B8%99/MapServer/{Layer_ID}
```

### 6.2 ข้อจำกัดทั่วไป

- **Max Record Count:** แต่ละ request ได้สูงสุด 1,000-2,000 records — ต้องใช้ pagination สำหรับข้อมูลขนาดใหญ่
- **403 บน cpudgiapp:** ต้องใส่ `?f=json` เสมอ — ไม่มี HTML viewer
- **Spatial Reference:** Layer จาก cpudgiapp ใช้ UTM Zone 47N (WKID 32647) — ใช้ `outSR=4326` เพื่อแปลงเป็น Lat/Lon
- **Rate Limiting:** ไม่มีข้อมูลชัดเจนเรื่อง rate limit — ควรใช้ delay ระหว่าง requests ถ้าดึงข้อมูลจำนวนมาก
- **Thai Encoding:** URL ที่มีภาษาไทยต้อง URL-encode (เช่น `จุดสนับสนุนสถานที่` → `%E0%B8%88%E0%B8%B8%E0%B8%94...`)

### 6.3 Spatial Reference Quick Reference

| WKID | ชื่อ | ใช้โดย |
|------|------|--------|
| 4326 | WGS 84 (Lat/Lon) | bmamap, bmagis (hospital) |
| 32647 | UTM Zone 47N | cpudgiapp (basemap, traffic, community) |
| 102100 | Web Mercator | bmagis (PM2.5) |

ใช้ parameter `outSR=4326` เพื่อแปลงผลลัพธ์ทั้งหมดให้เป็น WGS 84 Lat/Lon ซึ่งใช้งานง่ายที่สุด
