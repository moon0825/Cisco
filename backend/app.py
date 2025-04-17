# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify, send_from_directory
from flask_restful import Api, Resource
from flask_cors import CORS
import os
import json
import time
from datetime import datetime, timedelta, timezone # timezone 추가
import random
import firebase_admin
from firebase_admin import credentials, firestore
# Firestore 특정 예외나 FieldFilter 등을 사용하려면 추가 import 필요
# from google.cloud.firestore_v1.base_query import FieldFilter
# from google.api_core import exceptions as google_exceptions

# --- Firebase 초기화 ---
SERVICE_ACCOUNT_KEY_PATH = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
db = None
try:
    # 서비스 계정 키 경로가 유효한지 확인
    if not SERVICE_ACCOUNT_KEY_PATH or not os.path.exists(SERVICE_ACCOUNT_KEY_PATH):
        raise ValueError("GOOGLE_APPLICATION_CREDENTIALS 환경 변수가 설정되지 않았거나, 해당 경로에 서비스 계정 키 파일이 없습니다.")

    # 앱이 이미 초기화되었는지 확인 (서버리스 환경에서 중요)
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT_KEY_PATH)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK 초기화 성공")
    else:
        print("Firebase Admin SDK 이미 초기화됨")

    db = firestore.client() # Firestore 클라이언트 가져오기
    print("Firebase Firestore 연결 성공")
except Exception as e:
    print(f"!!! Firebase 초기화 오류: {e} !!! Firestore 기능이 비활성화됩니다.")
    db = None # 오류 발생 시 db를 None으로 설정

# --- Flask 앱 설정 ---
app = Flask(__name__)
# CORS 설정: 실제 배포 시에는 허용할 출처(프론트엔드 주소)를 명시하는 것이 안전합니다.
CORS(app, origins=[os.environ.get("FRONTEND_URL", "*")], supports_credentials=True) # 모든 출처 허용 (개발용) 또는 환경변수 사용
api = Api(app)


# --- Webex 통합 설정 ---
# (Webex 관련 코드는 Firestore와 직접적인 연관이 없으므로 기존 로직 유지)
try:
    # webex_integration 모듈 위치 주의 (backend 폴더 안에 있다면)
    from webex_integration import WebexAPI, MedicalWebexIntegration
except ImportError:
    print("경고: webex_integration.py 모듈을 찾을 수 없습니다. 경로를 확인하세요. Webex 기능이 시뮬레이션됩니다.")
    # 시뮬레이션을 위한 더미 클래스 정의... (기존 코드 유지)
    class WebexAPI:
        def __init__(self, access_token=None): pass
        def get_user_info(self): return {"displayName": "Simulated User"}
    class MedicalWebexIntegration:
         def __init__(self, webex_api): self.webex_api = webex_api
         def create_emergency_session(self, **kwargs): print("시뮬레이션: 긴급 세션 생성", kwargs); return {"id": "sim_session"}
         def send_glucose_alert(self, **kwargs): print("시뮬레이션: 혈당 알림 전송", kwargs); return {"id": "sim_msg"}
         def schedule_regular_checkup(self, **kwargs): print("시뮬레이션: 정기 검진 예약", kwargs); return {"id": "sim_meeting"}


webex_api = None
medical_webex = None
webex_token = os.environ.get("WEBEX_ACCESS_TOKEN")

if not webex_token:
    print("------------------------------------------------------------")
    print("경고: WEBEX_ACCESS_TOKEN 환경 변수가 설정되지 않았습니다. Webex 연동 기능이 시뮬레이션 모드로 작동합니다.")
    print("------------------------------------------------------------")
    webex_api_sim = WebexAPI()
    medical_webex = MedicalWebexIntegration(webex_api_sim)
else:
    try:
        webex_api = WebexAPI(access_token=webex_token)
        user_info = webex_api.get_user_info()
        print(f"Webex API 연결 성공: 사용자 '{user_info.get('displayName')}'")
        medical_webex = MedicalWebexIntegration(webex_api)
    except Exception as e:
        print(f"!!! Webex API 초기화 실패: {e} !!! Webex 연동 기능이 시뮬레이션 모드로 작동합니다.")
        webex_api_sim = WebexAPI()
        medical_webex = MedicalWebexIntegration(webex_api_sim)


# --- Helper Function ---
def firestore_timestamp_to_iso(timestamp):
    """Firestore 타임스탬프 객체를 ISO 8601 문자열로 변환"""
    if timestamp and hasattr(timestamp, 'isoformat'):
        # Firestore 타임스탬프는 이미 UTC일 수 있으므로 timezone 정보 확인
        if timestamp.tzinfo is None:
             timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.isoformat(timespec='seconds') # 초 단위까지
    return None

# --- API 리소스 정의 (Firestore 사용) ---

class PatientResource(Resource):
    def get(self, patient_id):
        if not db: return {"error": "Database service unavailable"}, 503
        try:
            patient_ref = db.collection('patients').document(patient_id)
            doc = patient_ref.get()
            if doc.exists:
                return doc.to_dict(), 200
            else:
                return {"error": "Patient not found"}, 404
        except Exception as e:
            print(f"Error getting patient {patient_id}: {e}")
            return {"error": "Internal server error fetching patient data"}, 500

    # 환자 생성/수정 위한 POST/PUT 메소드 추가 가능

class GlucoseResource(Resource):
    def get(self, patient_id):
        if not db: return {"error": "Database service unavailable"}, 503
        try:
            hours = request.args.get('hours', default=24, type=int)
            # Firestore에서 시간 범위 쿼리는 복잡하므로, 최근 N개 조회로 단순화
            limit = min(hours * 12, 1000) # 5분 간격 가정, 최대 1000개 제한

            readings_query = db.collection('glucoseReadings') \
                                .where('patientId', '==', patient_id) \
                                .order_by('timestamp', direction=firestore.Query.DESCENDING) \
                                .limit(limit)

            docs = readings_query.stream()
            readings_list = []
            for doc in docs:
                data = doc.to_dict()
                # Firestore 타임스탬프를 ISO 문자열로 변환하여 프론트엔드 호환성 확보
                data['timestamp'] = firestore_timestamp_to_iso(data.get('timestamp'))
                readings_list.append(data)

            readings_list.reverse() # 시간 순서대로 정렬
            return {"readings": readings_list}, 200
        except Exception as e:
            print(f"Error getting glucose readings for {patient_id}: {e}")
            # Firestore 인덱스 오류 메시지 확인: e.details() 등에 정보 포함될 수 있음
            if "index" in str(e).lower():
                 return {"error": "Database query requires an index. Please create it in the Firestore console."}, 500
            return {"error": "Internal server error fetching glucose data"}, 500

    def post(self, patient_id):
        if not db: return {"error": "Database service unavailable"}, 503

        # 환자 존재 여부 확인 (선택적이지만 권장)
        patient_ref = db.collection('patients').document(patient_id)
        if not patient_ref.get().exists:
             return {"error": "Patient not found, cannot add reading"}, 404

        data = request.get_json()
        if not data or 'value' not in data:
            return {"error": "Invalid data format: 'value' is required"}, 400

        try:
            new_reading_data = {
                'patientId': patient_id,
                'value': data['value'],
                'unit': data.get('unit', 'mg/dL'),
                'source': data.get('source', 'Manual'),
                'timestamp': firestore.SERVER_TIMESTAMP # 서버 시간 사용
            }
            # 클라이언트 시간도 저장하려면:
            # if 'timestamp' in data:
            #     try:
            #         client_dt = datetime.strptime(data['timestamp'], "%Y-%m-%d %H:%M:%S")
            #         new_reading_data['client_timestamp'] = client_dt.replace(tzinfo=timezone.utc) # UTC로 가정
            #     except ValueError:
            #         print("경고: 클라이언트 타임스탬프 형식이 잘못되었습니다.")

            # Firestore에 문서 추가
            update_time, doc_ref = db.collection('glucoseReadings').add(new_reading_data)
            print(f'Added glucose reading with ID: {doc_ref.id} for patient {patient_id} at {update_time}')

            # 백그라운드에서 예측 및 알림 업데이트 트리거 (비동기 방식 고려)
            # 여기서는 단순 호출 (실제로는 Celery, Cloud Functions 등 사용 권장)
            self._trigger_prediction_update(patient_id)

            return {"message": "Glucose reading added", "id": doc_ref.id}, 201
        except Exception as e:
            print(f"Error adding glucose reading for {patient_id}: {e}")
            return {"error": "Internal server error adding glucose data"}, 500

    def _trigger_prediction_update(self, patient_id):
        # 예측 업데이트 및 알림 생성 로직 (Firestore 기반으로 수정 필요)
        print(f"백그라운드 예측/알림 업데이트 트리거: {patient_id}")
        # TODO: Firestore에서 최근 혈당 읽기 -> 예측 수행 -> Firestore에 예측 저장 -> 알림 생성/저장/Webex 전송
        # 이 로직은 별도 함수로 분리하는 것이 좋음
        try:
            self._run_prediction_and_alerting_logic(patient_id)
        except Exception as e:
            print(f"!!! 예측/알림 로직 실행 중 오류 ({patient_id}): {e} !!!")


    def _run_prediction_and_alerting_logic(self, patient_id):
        if not db:
            print("DB 연결 없음, 예측/알림 로직 건너<0xEB><0x81>니다.")
            return

        print(f"Firestore 기반 예측/알림 로직 실행 시작: {patient_id}")
        # 1. Firestore에서 최근 혈당 데이터 조회 (예: 최근 12개)
        recent_readings_query = db.collection('glucoseReadings') \
                                    .where('patientId', '==', patient_id) \
                                    .order_by('timestamp', direction=firestore.Query.DESCENDING) \
                                    .limit(12)
        docs = list(recent_readings_query.stream()) # 결과를 리스트로 받아옴
        if len(docs) < 12:
             print(f"경고: 예측 위한 혈당 데이터 부족 ({len(docs)}개), 예측/알림 건너<0xEB><0x81>니다.")
             # 예측 컬렉션에 '데이터 부족' 상태 저장 가능
             pred_ref = db.collection('predictions').document(patient_id)
             pred_ref.set({'status': 'insufficient_data', 'updated_at': firestore.SERVER_TIMESTAMP}, merge=True)
             return

        # 시간 순서대로 정렬 (오래된것 -> 최신)
        docs.reverse()
        recent_values = [doc.to_dict()['value'] for doc in docs]
        latest_reading_doc = docs[-1].to_dict()
        latest_reading_value = latest_reading_doc['value']
        latest_reading_ts = latest_reading_doc['timestamp']

        # 2. 예측 수행 (여기서는 시뮬레이션 유지, 실제로는 BiT-MAML 모델 사용)
        print(f"예측 시뮬레이션 수행: {patient_id}")
        # (기존 시뮬레이션 로직 활용) ...
        # 간단히 랜덤하게 계산
        trend_change = random.randint(-10, 10) # 임시 추세
        prediction_30min = max(40, min(300, latest_reading_value + trend_change + random.randint(-10, 10)))
        prediction_60min = max(40, min(300, prediction_30min + random.randint(-15, 15)))
        print(f"예측 결과: 30min={prediction_30min}, 60min={prediction_60min}")

        # 3. Firestore에 예측 결과 저장
        pred_ref = db.collection('predictions').document(patient_id)
        prediction_data = {
            "current": {
                "timestamp": latest_reading_ts, # Firestore 타임스탬프 객체
                "value": latest_reading_value
            },
            "prediction_30min": {
                # 예측 시간 계산 필요 (latest_reading_ts 기준)
                "timestamp": latest_reading_ts + timedelta(minutes=30) if latest_reading_ts else None,
                "value": prediction_30min
            },
            "prediction_60min": {
                "timestamp": latest_reading_ts + timedelta(minutes=60) if latest_reading_ts else None,
                "value": prediction_60min
            },
            "status": "success",
            "updated_at": firestore.SERVER_TIMESTAMP
        }
        pred_ref.set(prediction_data) # 덮어쓰기 (set) 또는 merge=True로 부분 업데이트
        print(f"Firestore 예측 저장 완료: {patient_id}")


        # 4. 위험 감지 및 알림 생성/Webex 전송
        patient_info_snap = db.collection('patients').document(patient_id).get()
        if not patient_info_snap.exists:
            print(f"경고: {patient_id} 환자 정보 Firestore에 없어 알림 생성 불가.")
            return
        patient_info = patient_info_snap.to_dict()
        target_range = patient_info.get("target_glucose_range", {"min": 70, "max": 180})

        alert_type = None
        predicted_value = None
        if prediction_30min < target_range["min"]:
             alert_type = "low"
             predicted_value = prediction_30min
        elif prediction_30min > target_range["max"]:
            alert_type = "high"
            predicted_value = prediction_30min

        if alert_type:
            self._create_and_send_alert_firestore(patient_id, patient_info, alert_type, predicted_value, 30, latest_reading_value, latest_reading_ts)
        else:
             print(f"정상 범위 예측, 알림 생성 안 함: {patient_id}")


    def _create_and_send_alert_firestore(self, patient_id, patient_info, alert_type, predicted_value, time_window, current_value, current_ts):
        """Firestore에 알림 저장 및 Webex 전송"""
        if not db: return

        print(f"알림 생성 시도 ({patient_id}, {alert_type})")
        try:
            # 중복 알림 방지 쿼리 (최근 10분 내 같은 유형)
            ten_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
            recent_alert_query = db.collection('alerts') \
                                    .where('patientId', '==', patient_id) \
                                    .where('type', '==', alert_type) \
                                    .where('timestamp', '>=', ten_minutes_ago) \
                                    .limit(1)
            if len(list(recent_alert_query.stream())) > 0:
                 print(f"중복 알림 방지: 최근 10분 내 동일 유형({alert_type}) 알림 존재 (Firestore)")
                 return

            # 알림 데이터 구성
            alert_message = f"{time_window}분 후 {alert_type} 혈당({predicted_value}mg/dL) 예측. 현재: {current_value}mg/dL"
            new_alert_data = {
                "patientId": patient_id,
                "type": alert_type,
                "predicted_value": predicted_value,
                "current_value": current_value,
                "time_window": time_window,
                "message": alert_message,
                "timestamp": firestore.SERVER_TIMESTAMP, # 알림 생성 시간
                "current_reading_timestamp": current_ts, # 관련 혈당 측정 시간
                "status": "active",
                "acknowledged": False
            }
            # Firestore에 알림 저장
            alert_ref = db.collection('alerts').add(new_alert_data)[1] # [1] is the DocumentReference
            print(f"Firestore 알림 저장 완료: ID={alert_ref.id}, 환자={patient_id}")

            # Webex 메시지 전송 로직
            if medical_webex:
                doctor_info_snap = db.collection('patients').document(patient_info.get("doctor_id", "")).get()
                if doctor_info_snap.exists:
                    doctor_info = doctor_info_snap.to_dict()
                    recommendation = "저혈당 위험! 15-20g의 탄수화물을 섭취하세요." if alert_type == "low" else "고혈당 위험! 필요시 의사와 상의하세요."
                    medical_webex.send_glucose_alert(
                         recipient_email=doctor_info.get("email"),
                         recipient_name=doctor_info.get("name"),
                         patient_name=patient_info.get("name"),
                         glucose_value=current_value,
                         prediction=predicted_value,
                         alert_type=f"{alert_type}_risk",
                         recommendation=f"환자({patient_info.get('name')}) {alert_message} {recommendation}",
                         alert_details_url=f"/patient/{patient_id}/dashboard" # 프론트엔드 URL 구조에 맞게 수정
                    )
                    print(f"Webex 알림 전송 완료 (의사: {doctor_info.get('email')})")
                else:
                     print(f"경고: 의사 정보({patient_info.get('doctor_id')}) Firestore에 없어 Webex 알림 전송 불가.")
            else:
                print("Webex 통합 비활성, 알림 메시지 전송 건너<0xEB><0x81>니다.")

        except Exception as e:
             print(f"!!! 알림 생성/전송 중 오류 발생 ({patient_id}): {e} !!!")


class PredictionResource(Resource):
    def get(self, patient_id):
        if not db: return {"error": "Database service unavailable"}, 503
        try:
            pred_ref = db.collection('predictions').document(patient_id)
            doc = pred_ref.get()
            if doc.exists:
                 data = doc.to_dict()
                 # 타임스탬프 변환
                 if 'current' in data and data['current']:
                     data['current']['timestamp'] = firestore_timestamp_to_iso(data['current'].get('timestamp'))
                 if 'prediction_30min' in data and data['prediction_30min']:
                     data['prediction_30min']['timestamp'] = firestore_timestamp_to_iso(data['prediction_30min'].get('timestamp'))
                 if 'prediction_60min' in data and data['prediction_60min']:
                     data['prediction_60min']['timestamp'] = firestore_timestamp_to_iso(data['prediction_60min'].get('timestamp'))
                 data['updated_at'] = firestore_timestamp_to_iso(data.get('updated_at'))
                 return data, 200
            else:
                 # 예측이 없는 경우, 실시간 생성을 시도할 수도 있으나 복잡함
                 # 여기서는 그냥 없다고 응답
                 return {"error": "No predictions available for this patient yet"}, 404
        except Exception as e:
            print(f"Error getting predictions for {patient_id}: {e}")
            return {"error": "Internal server error fetching predictions"}, 500


class AlertResource(Resource):
    def get(self, patient_id):
        if not db: return {"error": "Database service unavailable"}, 503
        try:
            active_only = request.args.get('active_only', default='true', type=str).lower() == 'true'
            limit = request.args.get('limit', default=10, type=int) # 최근 10개 제한

            alerts_query = db.collection('alerts').where('patientId', '==', patient_id)

            if active_only:
                alerts_query = alerts_query.where('status', '==', 'active')

            alerts_query = alerts_query.order_by('timestamp', direction=firestore.Query.DESCENDING).limit(limit)
            docs = alerts_query.stream()

            alerts_list = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id # 문서 ID 추가
                data['timestamp'] = firestore_timestamp_to_iso(data.get('timestamp'))
                data['current_reading_timestamp'] = firestore_timestamp_to_iso(data.get('current_reading_timestamp'))
                alerts_list.append(data)

            return {"alerts": alerts_list}, 200
        except Exception as e:
            print(f"Error getting alerts for {patient_id}: {e}")
            if "index" in str(e).lower():
                 return {"error": "Database query requires an index. Please create it in the Firestore console."}, 500
            return {"error": "Internal server error fetching alerts"}, 500

    def put(self, patient_id, alert_id):
        # patient_id는 URL 경로에 있지만 실제로는 alert_id로 문서를 찾음
        if not db: return {"error": "Database service unavailable"}, 503

        data = request.get_json()
        if not data or ('status' not in data and 'acknowledged' not in data):
            return {"error": "Invalid data format: 'status' or 'acknowledged' required"}, 400

        try:
            alert_ref = db.collection('alerts').document(alert_id)
            doc = alert_ref.get()

            if not doc.exists:
                return {"error": "Alert not found"}, 404

            # 접근 제어: 해당 patient_id의 알림이 맞는지 확인 (선택적이지만 권장)
            alert_data = doc.to_dict()
            if alert_data.get('patientId') != patient_id:
                 return {"error": "Alert does not belong to this patient"}, 403 # Forbidden

            update_data = {}
            if 'status' in data:
                 update_data['status'] = data['status']
            if 'acknowledged' in data:
                 update_data['acknowledged'] = data['acknowledged']

            if update_data:
                 update_data['acknowledged_at'] = firestore.SERVER_TIMESTAMP # 확인 시간 기록
                 alert_ref.update(update_data)
                 print(f"Alert updated ({alert_id}): {update_data}")
                 # 업데이트된 문서 다시 읽어서 반환
                 updated_doc = alert_ref.get().to_dict()
                 updated_doc['id'] = alert_id
                 updated_doc['timestamp'] = firestore_timestamp_to_iso(updated_doc.get('timestamp'))
                 updated_doc['acknowledged_at'] = firestore_timestamp_to_iso(updated_doc.get('acknowledged_at'))
                 return {"message": "Alert updated", "alert": updated_doc}, 200
            else:
                 return {"message": "No changes detected"}, 304 # Not Modified

        except Exception as e:
             print(f"Error updating alert {alert_id}: {e}")
             return {"error": "Internal server error updating alert"}, 500


# --- Webex 통합 API 엔드포인트 (Firestore 사용) ---
class WebexEmergencyConnect(Resource):
    def post(self):
        if not db: return {"error": "Database service unavailable"}, 503
        if not medical_webex: return {"error": "Webex integration is not available."}, 503

        data = request.get_json()
        if not data or 'patient_id' not in data:
            return {"error": "Patient ID is required"}, 400
        patient_id = data.get('patient_id')

        try:
            # Firestore에서 환자 및 의사 정보 조회
            patient_snap = db.collection('patients').document(patient_id).get()
            if not patient_snap.exists: return {"error": "Patient not found"}, 404
            patient_info = patient_snap.to_dict()

            doctor_id = patient_info.get("doctor_id")
            if not doctor_id: return {"error": "Doctor ID not assigned to patient"}, 400
            doctor_snap = db.collection('doctors').document(doctor_id).get() # 'doctors' 컬렉션 사용 가정
            # 또는 'patients' 컬렉션에 의사 정보 포함? -> 여기서는 doctors 컬렉션 가정
            if not doctor_snap.exists:
                 # doctors 컬렉션 대신 patients 컬렉션에서 의사 찾기 시도 (데모용 임시)
                 doctor_snap = db.collection('patients').document(doctor_id).get()
                 if not doctor_snap.exists or doctor_snap.to_dict().get('specialty') is None: # 의사인지 확인
                     return {"error": f"Doctor ({doctor_id}) not found"}, 404

            doctor_info = doctor_snap.to_dict()

            # Firestore에서 최신 예측 정보 조회
            pred_snap = db.collection('predictions').document(patient_id).get()
            current_glucose = "N/A"
            predicted_glucose = "N/A"
            if pred_snap.exists:
                pred_data = pred_snap.to_dict()
                current_glucose = pred_data.get('current', {}).get('value', 'N/A')
                predicted_glucose = pred_data.get('prediction_30min', {}).get('value', 'N/A')

            print(f"Webex 긴급 연결 시도: 환자={patient_info.get('name')}, 의사={doctor_info.get('name')}")
            session_info = medical_webex.create_emergency_session(
                patient_email=patient_info.get("email"),
                patient_name=patient_info.get("name"),
                glucose_value=current_glucose,
                prediction=predicted_glucose,
                doctor_email=doctor_info.get("email")
            )
            print(f"Webex 긴급 연결 성공: 세션 ID={session_info.get('id')}")
            return session_info, 201
        except Exception as e:
            print(f"Webex 긴급 연결 실패: {e}")
            return {"error": f"Failed to create Webex emergency session: {str(e)}"}, 500


class WebexScheduleCheckup(Resource):
    def post(self):
        if not db: return {"error": "Database service unavailable"}, 503
        if not medical_webex: return {"error": "Webex integration is not available."}, 503

        data = request.get_json()
        required_fields = ['patient_id', 'start_time']
        if not data or not all(field in data for field in required_fields):
            return {"error": f"Missing required fields: {required_fields}"}, 400

        patient_id = data.get('patient_id')
        start_time_str = data.get('start_time') # ISO 8601 형식
        duration = data.get('duration_minutes', 30)
        notes = data.get('notes')

        try:
            # 시간 형식 검증
            datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))

            # Firestore에서 환자 및 의사 정보 조회
            patient_snap = db.collection('patients').document(patient_id).get()
            if not patient_snap.exists: return {"error": "Patient not found"}, 404
            patient_info = patient_snap.to_dict()

            doctor_id = patient_info.get("doctor_id")
            if not doctor_id: return {"error": "Doctor ID not assigned to patient"}, 400
            doctor_snap = db.collection('doctors').document(doctor_id).get() # 'doctors' 컬렉션 가정
            if not doctor_snap.exists:
                 # 임시: patients 컬렉션에서 의사 찾기
                 doctor_snap = db.collection('patients').document(doctor_id).get()
                 if not doctor_snap.exists or doctor_snap.to_dict().get('specialty') is None:
                     return {"error": f"Doctor ({doctor_id}) not found"}, 404
            doctor_info = doctor_snap.to_dict()

            print(f"Webex 정기 검진 예약 시도: 환자={patient_info.get('name')}, 의사={doctor_info.get('name')}, 시간={start_time_str}")
            meeting_info = medical_webex.schedule_regular_checkup(
                patient_email=patient_info.get("email"),
                patient_name=patient_info.get("name"),
                doctor_email=doctor_info.get("email"),
                doctor_name=doctor_info.get("name"),
                start_time=start_time_str,
                duration_minutes=duration,
                notes=notes
            )
            print(f"Webex 정기 검진 예약 성공: 미팅 ID={meeting_info.get('id')}")
            return {
                "message": "Meeting scheduled successfully",
                "meeting_id": meeting_info.get('id'),
                "title": meeting_info.get('title'),
                "start_time": meeting_info.get('start'),
                "end_time": meeting_info.get('end'),
                "join_url": meeting_info.get('webLink')
            }, 201
        except ValueError:
             return {"error": "Invalid start_time format. Use ISO 8601 format (e.g., YYYY-MM-DDTHH:MM:SSZ)."}, 400
        except Exception as e:
            print(f"Webex 정기 검진 예약 실패: {e}")
            return {"error": f"Failed to schedule Webex meeting: {str(e)}"}, 500


# --- 데모 데이터 생성 API (Firestore용) ---
class SeedDemoData(Resource):
     def post(self):
         if not db: return {"error": "Database service unavailable"}, 503
         print("*** 데모 데이터 Firestore 시딩 시작 ***")
         try:
             # 샘플 환자 데이터 (Firestore 문서 ID를 patient1로 지정)
             patient1_data = {
                 # "id": "patient1", # 문서 ID로 사용하므로 필드에서는 제거 가능
                 "name": "김민수", "age": 28, "type": "1형 당뇨",
                 "diagnosis_date": "2018-05-12", "doctor_id": "doctor1",
                 "insulin_regimen": "Multiple daily injections",
                 "target_glucose_range": {"min": 70, "max": 180},
                 "email": os.environ.get("TEST_PATIENT_EMAIL", "patient1_default@example.com")
             }
             db.collection('patients').document('patient1').set(patient1_data)
             print("환자 'patient1' 데이터 생성 완료")

             # 샘플 의사 데이터 (Firestore 문서 ID를 doctor1로 지정)
             doctor1_data = {
                 # "id": "doctor1",
                 "name": "이지원", "specialty": "내분비내과",
                 "hospital": "서울대학교병원", "patients": ["patient1"], # 이 필드는 필요 없을 수 있음
                 "email": os.environ.get("TEST_DOCTOR_EMAIL", "doctor1_default@example.com")
             }
             # 'doctors' 컬렉션 사용 권장. 여기서는 임시로 'patients' 사용
             db.collection('patients').document('doctor1').set(doctor1_data)
             # db.collection('doctors').document('doctor1').set(doctor1_data) # 이상적
             print("의사 'doctor1' 데이터 생성 완료")

             # 샘플 혈당 데이터 생성 (최근 1시간 정도만 생성)
             now = datetime.now(timezone.utc) # UTC 기준 시간
             batch = db.batch() # 배치 쓰기로 효율성 증대
             glucose_docs_count = 0
             current_glucose = 118
             for i in range(12): # 최근 1시간 (5분 간격)
                 timestamp = now - timedelta(minutes=5 * i)
                 change = random.randint(-5, 5)
                 current_glucose += change
                 current_glucose = max(40, min(300, current_glucose))
                 reading_data = {
                     'patientId': 'patient1', 'value': current_glucose,
                     'unit': 'mg/dL', 'source': 'CGM_Demo',
                     'timestamp': timestamp # Firestore는 datetime 객체 직접 저장 가능
                 }
                 doc_ref = db.collection('glucoseReadings').document() # 자동 ID
                 batch.set(doc_ref, reading_data)
                 glucose_docs_count += 1
             batch.commit() # 배치 쓰기 실행
             print(f"혈당 데이터 {glucose_docs_count}개 생성 완료 (patient1)")

             # 초기 예측 생성 트리거
             print("초기 예측 생성 시도...")
             temp_glucose_res = GlucoseResource()
             temp_glucose_res._trigger_prediction_update('patient1')


             return {"message": "Demo data seeding successful!"}, 201

         except Exception as e:
             print(f"!!! 데모 데이터 시딩 중 오류: {e} !!!")
             return {"error": f"Failed to seed demo data: {str(e)}"}, 500


# --- API 라우트 등록 ---
api.add_resource(PatientResource, '/api/patients/<string:patient_id>')
api.add_resource(GlucoseResource, '/api/patients/<string:patient_id>/glucose')
api.add_resource(PredictionResource, '/api/patients/<string:patient_id>/predictions')
api.add_resource(AlertResource, '/api/patients/<string:patient_id>/alerts', '/api/patients/<string:patient_id>/alerts/<string:alert_id>')
api.add_resource(WebexEmergencyConnect, '/api/webex/emergency_connect')
api.add_resource(WebexScheduleCheckup, '/api/webex/schedule_checkup')
api.add_resource(SeedDemoData, '/api/seed_demo_data') # 데모 데이터 생성 엔드포인트

# 서버 상태 확인 엔드포인트
@app.route('/api/status', methods=['GET'])
def status():
    webex_status = "Not Configured"
    # ... (Webex 상태 로직은 동일) ...
    db_status = "Connected" if db else "Disconnected"

    return jsonify({
        "status": "online",
        "version": "0.3.0-firestore", # 버전 업데이트
        "timestamp": datetime.now().isoformat(),
        "webex_status": webex_status,
        "database_status": db_status # DB 연결 상태 추가
    })

# 프론트엔드 제공 라우트
@app.route('/')
def serve_index():
    # app.py와 같은 폴더에 frontend 폴더가 있고 그 안에 index.html이 있다고 가정
    # 또는 Vercel에서는 보통 프론트/백엔드를 분리하므로 이 라우트가 필요 없을 수 있음
    # 여기서는 로컬 테스트를 위해 남겨둠
    print("Serving index.html")
    return send_from_directory('.', 'index.html') # 필요시 경로 수정

# --- 메인 실행 ---
if __name__ == '__main__':
    # 전역 범위에서의 데모 데이터 초기화 제거
    # print("Initializing demo data...")
    # initialize_demo_data() # <-- 제거!
    # print("Demo data initialized.")

    # 서버 실행
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting server on port {port}...")
    # debug=True는 Vercel 배포 시에는 False로 설정하는 것이 좋음
    app.run(host='0.0.0.0', port=port, debug=False if os.environ.get('VERCEL') else True)