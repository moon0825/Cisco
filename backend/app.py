# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify # send_from_directory 제거
from flask_restful import Api, Resource
from flask_cors import CORS
import os
import json
import time
from datetime import datetime, timedelta, timezone
import random
import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core import exceptions as google_exceptions

# --- Firebase 초기화 ---
# GOOGLE_APPLICATION_CREDENTIALS 환경 변수 또는 기본 경로의 키 파일을 사용하여 초기화 시도
SERVICE_ACCOUNT_KEY_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./ciscoglucose-firebase-adminsdk-fbsvc-3864a20e01.json")
db = None
try:
    print(f"서비스 계정 키 파일 경로 확인: {SERVICE_ACCOUNT_KEY_PATH}")
    if not os.path.exists(SERVICE_ACCOUNT_KEY_PATH):
        raise FileNotFoundError(f"서비스 계정 키 파일이 존재하지 않습니다: {SERVICE_ACCOUNT_KEY_PATH}")
    if not os.access(SERVICE_ACCOUNT_KEY_PATH, os.R_OK):
         raise PermissionError(f"서비스 계정 키 파일 읽기 권한 없음: {SERVICE_ACCOUNT_KEY_PATH}")

    # 앱 중복 초기화 방지
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT_KEY_PATH)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK 초기화 성공")
    else:
        print("Firebase Admin SDK 이미 초기화됨")
    db = firestore.client()
    print("Firebase Firestore 클라이언트 생성 및 연결 성공")
    # 간단한 연결 테스트 (옵션)
    # db.collection('__test__').document('conn').set({'timestamp': firestore.SERVER_TIMESTAMP})
    # print("Firestore 쓰기 테스트 완료 (무시해도 됨)")
except Exception as e:
    print(f"!!! Firebase 초기화 중 심각한 오류 발생: {e} !!!")
    print("!!! Firestore 기능이 비활성화됩니다. 서비스 계정 키 경로 및 권한, 파일 형식을 확인하세요. !!!")
    db = None

# --- Flask 앱 설정 ---
app = Flask(__name__)
# CORS 설정: 프론트엔드 Vercel 주소를 명시적으로 허용하는 것이 좋음
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://cisco-git-main-kihoon-moons-projects.vercel.app") # 기본값 설정
CORS(app, origins=[FRONTEND_URL, "http://localhost:5000"], supports_credentials=True) # 로컬 및 배포 주소 허용
api = Api(app)

# --- Webex 통합 설정 (기존 코드 유지) ---
try:
    from webex_integration import WebexAPI, MedicalWebexIntegration
except ImportError:
    print("경고: webex_integration.py 모듈을 찾을 수 없습니다. Webex 기능이 시뮬레이션됩니다.")
    class WebexAPI: # Dummy
        def __init__(self, access_token=None): pass
        def get_user_info(self): return {"displayName": "Simulated User"}
    class MedicalWebexIntegration: # Dummy
         def __init__(self, webex_api): self.webex_api = webex_api
         def create_emergency_session(self, **kwargs): print("[SIM] 긴급 세션 생성:", kwargs); return {"id": "sim_session"}
         def send_glucose_alert(self, **kwargs): print("[SIM] 혈당 알림:", kwargs); return {"id": "sim_msg"}
         def schedule_regular_checkup(self, **kwargs): print("[SIM] 정기 검진 예약:", kwargs); return {"id": "sim_meeting"}

webex_api = None
medical_webex = None
webex_token = os.environ.get("WEBEX_ACCESS_TOKEN")

if not webex_token:
    print("Webex: ACCESS_TOKEN 없음. 시뮬레이션 모드.")
    webex_api_sim = WebexAPI()
    medical_webex = MedicalWebexIntegration(webex_api_sim)
else:
    try:
        webex_api = WebexAPI(access_token=webex_token)
        user_info = webex_api.get_user_info()
        print(f"Webex API 연결 성공: 사용자 '{user_info.get('displayName')}'")
        medical_webex = MedicalWebexIntegration(webex_api)
    except Exception as e:
        print(f"!!! Webex API 초기화 실패: {e} !!! 시뮬레이션 모드.")
        webex_api_sim = WebexAPI()
        medical_webex = MedicalWebexIntegration(webex_api_sim)

# --- Firestore 컬렉션 이름 상수화 ---
PATIENTS_COLLECTION = 'patients'
GLUCOSE_COLLECTION = 'glucoseReadings'
PREDICTIONS_COLLECTION = 'predictions'
ALERTS_COLLECTION = 'alerts'
# DOCTORS_COLLECTION = 'doctors' # 의사 정보 별도 관리 시

# --- Helper Function (기존 코드 유지) ---
def firestore_timestamp_to_iso(timestamp):
    if timestamp and hasattr(timestamp, 'isoformat'):
        if timestamp.tzinfo is None:
             timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.isoformat(timespec='seconds')
    return None

# --- API 리소스 정의 (Firestore 사용) ---

class PatientResource(Resource):
    """환자 정보 API"""
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            patient_ref = db.collection(PATIENTS_COLLECTION).document(patient_id)
            doc = patient_ref.get()
            if doc.exists:
                return doc.to_dict(), 200
            else:
                return {"error": "환자 없음"}, 404
        except Exception as e:
            print(f"환자({patient_id}) 조회 오류: {e}")
            return {"error": "서버 오류 (환자 조회)"}, 500

class GlucoseResource(Resource):
    """혈당 데이터 API"""
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            hours = request.args.get('hours', default=24, type=int)
            limit = min(hours * 12, 1000) # 최대 1000개 조회

            readings_query = db.collection(GLUCOSE_COLLECTION) \
                                .where('patientId', '==', patient_id) \
                                .order_by('timestamp', direction=firestore.Query.DESCENDING) \
                                .limit(limit)

            docs = readings_query.stream()
            readings_list = []
            for doc in docs:
                data = doc.to_dict()
                data['timestamp'] = firestore_timestamp_to_iso(data.get('timestamp'))
                # 프론트엔드가 value 필드를 사용한다면 맞춰주기 (Firestore에는 value로 저장 가정)
                # data['glucose'] = data.get('value', 0) # 필요시 추가
                readings_list.append(data)

            readings_list.reverse() # 시간순 정렬
            print(f"환자({patient_id}) 혈당 {len(readings_list)}개 조회 완료 (최대 {limit}개)")
            return {"readings": readings_list}, 200

        except google_exceptions.NotFound as e:
            print(f"Firestore 컬렉션({GLUCOSE_COLLECTION}) 없음 오류: {e}")
            return {"error": f"DB 오류: '{GLUCOSE_COLLECTION}' 컬렉션 없음"}, 500
        except google_exceptions.FailedPrecondition as e:
             # Firestore 인덱스 부족 오류 감지
             if "index" in str(e).lower():
                 print(f"!!! Firestore 인덱스 필요 오류: {e} !!!")
                 return {"error": "DB 쿼리 인덱스 필요. Firestore 콘솔에서 생성하세요."}, 400 # 400 Bad Request 또는 500
             else:
                 print(f"Firestore 조건 오류 ({patient_id}): {e}")
                 return {"error": "DB 조건 오류"}, 500
        except Exception as e:
            print(f"혈당({patient_id}) 조회 오류: {e}")
            return {"error": f"서버 오류 (혈당 조회): {str(e)}"}, 500

    def post(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            # 환자 존재 확인
            patient_ref = db.collection(PATIENTS_COLLECTION).document(patient_id)
            if not patient_ref.get().exists:
                return {"error": "환자 없음"}, 404

            data = request.get_json()
            if not data or 'value' not in data:
                return {"error": "잘못된 형식: 'value' 필수"}, 400

            new_reading_data = {
                'patientId': patient_id,
                'value': data['value'],
                'unit': data.get('unit', 'mg/dL'),
                'source': data.get('source', 'Manual'),
                'timestamp': firestore.SERVER_TIMESTAMP
            }
            update_time, doc_ref = db.collection(GLUCOSE_COLLECTION).add(new_reading_data)
            print(f'혈당 추가 완료: ID={doc_ref.id}, 환자={patient_id} at {update_time}')

            # 예측/알림 업데이트 트리거
            self._trigger_prediction_update(patient_id)

            return {"message": "혈당 추가 성공", "id": doc_ref.id}, 201
        except Exception as e:
            print(f"혈당({patient_id}) 추가 오류: {e}")
            return {"error": "서버 오류 (혈당 추가)"}, 500

    def _trigger_prediction_update(self, patient_id):
        """예측/알림 로직 실행 (Firestore 기반)"""
        print(f"예측/알림 업데이트 트리거: {patient_id}")
        try:
            # 이 함수는 실제로는 백그라운드에서 비동기로 실행하는 것이 좋음
            self._run_prediction_and_alerting_logic(patient_id)
        except Exception as e:
            print(f"!!! 예측/알림 로직 실행 중 오류 ({patient_id}): {e} !!!")

    def _run_prediction_and_alerting_logic(self, patient_id):
        """Firestore에서 데이터 읽고 예측/알림 저장 및 Webex 전송"""
        if not db: return

        print(f"Firestore 예측/알림 로직 시작: {patient_id}")
        pred_ref = db.collection(PREDICTIONS_COLLECTION).document(patient_id)
        alert_collection = db.collection(ALERTS_COLLECTION)
        patient_ref = db.collection(PATIENTS_COLLECTION).document(patient_id)

        try:
            # 1. 최근 혈당 데이터 조회
            readings_query = db.collection(GLUCOSE_COLLECTION) \
                                .where('patientId', '==', patient_id) \
                                .order_by('timestamp', direction=firestore.Query.DESCENDING).limit(12)
            docs = list(readings_query.stream())
            if len(docs) < 1: # 예측에 최소 1개 데이터 필요 (실제로는 더 많이 필요)
                print(f"예측 위한 혈당 데이터 부족 ({len(docs)}개)")
                pred_ref.set({'status': 'insufficient_data', 'updated_at': firestore.SERVER_TIMESTAMP}, merge=True)
                return

            docs.reverse() # 시간순
            latest_reading_doc = docs[-1].to_dict()
            latest_reading_value = latest_reading_doc.get('value')
            latest_reading_ts = latest_reading_doc.get('timestamp')

            if latest_reading_value is None or latest_reading_ts is None:
                 print("최신 혈당 값 또는 타임스탬프 누락")
                 return

            # 2. 예측 수행 (시뮬레이션)
            # TODO: 실제 BiT-MAML 모델 연동 필요
            print(f"예측 시뮬레이션 수행: {patient_id}")
            trend_change = random.randint(-10, 10)
            prediction_30min = max(40, min(300, latest_reading_value + trend_change + random.randint(-10, 10)))
            prediction_60min = max(40, min(300, prediction_30min + random.randint(-15, 15)))
            print(f"예측 결과: 30min={prediction_30min}, 60min={prediction_60min}")

            # 3. Firestore에 예측 결과 저장
            prediction_data = {
                "current": {"timestamp": latest_reading_ts, "value": latest_reading_value},
                "prediction_30min": {"timestamp": latest_reading_ts + timedelta(minutes=30), "value": prediction_30min},
                "prediction_60min": {"timestamp": latest_reading_ts + timedelta(minutes=60), "value": prediction_60min},
                "status": "success", "updated_at": firestore.SERVER_TIMESTAMP
            }
            pred_ref.set(prediction_data)
            print(f"Firestore 예측 저장 완료: {patient_id}")

            # 4. 위험 감지 및 알림 처리
            patient_snap = patient_ref.get()
            if not patient_snap.exists: return
            patient_info = patient_snap.to_dict()
            target_range = patient_info.get("target_glucose_range", {"min": 70, "max": 180})

            alert_type = None
            predicted_value = None
            if prediction_30min < target_range["min"]: alert_type = "low"; predicted_value = prediction_30min
            elif prediction_30min > target_range["max"]: alert_type = "high"; predicted_value = prediction_30min

            if alert_type:
                # 중복 알림 방지 쿼리 (최근 10분)
                ten_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
                recent_alert_query = alert_collection.where('patientId', '==', patient_id) \
                                        .where('type', '==', alert_type) \
                                        .where('timestamp', '>=', ten_minutes_ago).limit(1)
                if len(list(recent_alert_query.stream())) > 0:
                    print(f"중복 알림 방지({alert_type})")
                    return

                # 알림 저장
                alert_message = f"30분 후 {alert_type} 혈당({predicted_value}mg/dL) 예측. 현재: {latest_reading_value}mg/dL"
                new_alert_data = {
                    "patientId": patient_id, "type": alert_type,
                    "predicted_value": predicted_value, "current_value": latest_reading_value,
                    "time_window": 30, "message": alert_message,
                    "timestamp": firestore.SERVER_TIMESTAMP,
                    "current_reading_timestamp": latest_reading_ts,
                    "status": "active", "acknowledged": False
                }
                alert_ref = alert_collection.add(new_alert_data)[1]
                print(f"Firestore 알림 저장: ID={alert_ref.id}")

                # Webex 전송
                if medical_webex:
                    doctor_id = patient_info.get("doctor_id")
                    doctor_snap = db.collection(PATIENTS_COLLECTION).document(doctor_id).get() # 임시: patients에서 의사 조회
                    if doctor_snap.exists:
                        doctor_info = doctor_snap.to_dict()
                        recommendation = "저혈당 위험! ..." if alert_type == "low" else "고혈당 위험! ..."
                        medical_webex.send_glucose_alert(
                             recipient_email=doctor_info.get("email"),
                             recipient_name=doctor_info.get("name"),
                             patient_name=patient_info.get("name"),
                             glucose_value=latest_reading_value,
                             prediction=predicted_value,
                             alert_type=f"{alert_type}_risk",
                             recommendation=f"환자({patient_info.get('name')}) {alert_message} {recommendation}",
                             alert_details_url=f"/patient/{patient_id}/dashboard" # 실제 URL 필요
                        )
                        print(f"Webex 알림 전송 완료 (의사: {doctor_info.get('email')})")
                    else: print(f"의사({doctor_id}) 정보 없음")
            else:
                print(f"정상 범위 예측, 알림 생성 안 함: {patient_id}")

        except Exception as e:
            print(f"!!! _run_prediction_and_alerting_logic 오류 ({patient_id}): {e} !!!")
            # 예측 상태를 오류로 업데이트 가능
            pred_ref.set({'status': 'error', 'error_message': str(e), 'updated_at': firestore.SERVER_TIMESTAMP}, merge=True)


class PredictionResource(Resource):
    """예측 정보 API"""
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            pred_ref = db.collection(PREDICTIONS_COLLECTION).document(patient_id)
            doc = pred_ref.get()
            if doc.exists:
                 data = doc.to_dict()
                 # 타임스탬프 변환
                 for key in ['current', 'prediction_30min', 'prediction_60min']:
                     if key in data and data[key] and 'timestamp' in data[key]:
                         data[key]['timestamp'] = firestore_timestamp_to_iso(data[key].get('timestamp'))
                 data['updated_at'] = firestore_timestamp_to_iso(data.get('updated_at'))
                 return data, 200
            else:
                 return {"error": "아직 예측 정보 없음"}, 404
        except Exception as e:
            print(f"예측({patient_id}) 조회 오류: {e}")
            return {"error": "서버 오류 (예측 조회)"}, 500


class AlertResource(Resource):
    """알림 정보 API"""
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            active_only = request.args.get('active_only', default='true', type=str).lower() == 'true'
            limit = request.args.get('limit', default=10, type=int)

            alerts_query = db.collection(ALERTS_COLLECTION).where('patientId', '==', patient_id)
            if active_only:
                alerts_query = alerts_query.where('status', '==', 'active')
            alerts_query = alerts_query.order_by('timestamp', direction=firestore.Query.DESCENDING).limit(limit)

            docs = alerts_query.stream()
            alerts_list = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                data['timestamp'] = firestore_timestamp_to_iso(data.get('timestamp'))
                data['current_reading_timestamp'] = firestore_timestamp_to_iso(data.get('current_reading_timestamp'))
                alerts_list.append(data)
            return {"alerts": alerts_list}, 200
        except google_exceptions.FailedPrecondition as e:
             if "index" in str(e).lower():
                 print(f"!!! Firestore 인덱스 필요 오류 (알림 조회): {e} !!!")
                 return {"error": "DB 쿼리 인덱스 필요. Firestore 콘솔에서 생성하세요."}, 400
             else: raise e # 다른 FailedPrecondition 오류 처리
        except Exception as e:
            print(f"알림({patient_id}) 조회 오류: {e}")
            return {"error": "서버 오류 (알림 조회)"}, 500

    def put(self, patient_id, alert_id):
        if not db: return {"error": "DB 미연결"}, 503
        data = request.get_json()
        if not data or ('status' not in data and 'acknowledged' not in data):
            return {"error": "잘못된 형식: 'status' 또는 'acknowledged' 필수"}, 400
        try:
            alert_ref = db.collection(ALERTS_COLLECTION).document(alert_id)
            doc_snap = alert_ref.get()
            if not doc_snap.exists: return {"error": "알림 없음"}, 404

            # 환자 ID 검증
            alert_data = doc_snap.to_dict()
            if alert_data.get('patientId') != patient_id:
                return {"error": "환자 권한 없음"}, 403

            update_data = {}
            if 'status' in data: update_data['status'] = data['status']
            if 'acknowledged' in data: update_data['acknowledged'] = data['acknowledged']

            if update_data:
                update_data['acknowledged_at'] = firestore.SERVER_TIMESTAMP
                alert_ref.update(update_data)
                print(f"알림 업데이트 완료 ({alert_id}): {update_data}")
                # 업데이트된 데이터 반환
                updated_doc = alert_ref.get().to_dict()
                updated_doc['id'] = alert_id
                updated_doc['timestamp'] = firestore_timestamp_to_iso(updated_doc.get('timestamp'))
                updated_doc['acknowledged_at'] = firestore_timestamp_to_iso(updated_doc.get('acknowledged_at'))
                return {"message": "알림 업데이트 성공", "alert": updated_doc}, 200
            else:
                return {"message": "변경 사항 없음"}, 304
        except Exception as e:
             print(f"알림({alert_id}) 업데이트 오류: {e}")
             return {"error": "서버 오류 (알림 업데이트)"}, 500


# --- Webex 통합 API 엔드포인트 (Firestore 사용) ---
class WebexEmergencyConnect(Resource):
    def post(self):
        if not db: return {"error": "DB 미연결"}, 503
        if not medical_webex: return {"error": "Webex 통합 비활성"}, 503

        data = request.get_json()
        if not data or 'patient_id' not in data: return {"error": "patient_id 필수"}, 400
        patient_id = data.get('patient_id')

        try:
            patient_snap = db.collection(PATIENTS_COLLECTION).document(patient_id).get()
            if not patient_snap.exists: return {"error": "환자 없음"}, 404
            patient_info = patient_snap.to_dict()

            doctor_id = patient_info.get("doctor_id")
            if not doctor_id: return {"error": "담당 의사 미지정"}, 400
            # 임시: patients 컬렉션에서 의사 조회
            doctor_snap = db.collection(PATIENTS_COLLECTION).document(doctor_id).get()
            if not doctor_snap.exists: return {"error": f"의사({doctor_id}) 없음"}, 404
            doctor_info = doctor_snap.to_dict()

            pred_snap = db.collection(PREDICTIONS_COLLECTION).document(patient_id).get()
            current_glucose = pred_snap.to_dict().get('current', {}).get('value', 'N/A') if pred_snap.exists else 'N/A'
            predicted_glucose = pred_snap.to_dict().get('prediction_30min', {}).get('value', 'N/A') if pred_snap.exists else 'N/A'

            print(f"Webex 긴급 연결 시도: 환자={patient_info.get('name')}, 의사={doctor_info.get('name')}")
            session_info = medical_webex.create_emergency_session(
                patient_email=patient_info.get("email"), patient_name=patient_info.get("name"),
                glucose_value=current_glucose, prediction=predicted_glucose,
                doctor_email=doctor_info.get("email")
            )
            print(f"Webex 긴급 연결 성공: 세션 ID={session_info.get('id')}")
            return session_info, 201
        except Exception as e:
            print(f"!!! Webex 긴급 연결 실패: {e} !!!")
            return {"error": f"Webex 긴급 연결 실패: {str(e)}"}, 500

class WebexScheduleCheckup(Resource):
     def post(self):
         if not db: return {"error": "DB 미연결"}, 503
         if not medical_webex: return {"error": "Webex 통합 비활성"}, 503
         # ... (Firestore에서 환자/의사 정보 조회 로직은 WebexEmergencyConnect와 유사) ...
         # TODO: WebexEmergencyConnect 코드 참조하여 구현
         return {"error": "Not Implemented Yet"}, 501


# --- 데모 데이터 생성 API (Firestore용) ---
class SeedDemoData(Resource):
     def post(self):
         if not db: return {"error": "DB 미연결"}, 503
         print("*** 데모 데이터 Firestore 시딩 시작 ***")
         try:
             patient1_ref = db.collection(PATIENTS_COLLECTION).document('patient1')
             patient1_data = {
                 "name": "김민수", "age": 28, "type": "1형 당뇨", "diagnosis_date": "2018-05-12",
                 "doctor_id": "doctor1", "insulin_regimen": "MDI",
                 "target_glucose_range": {"min": 70, "max": 180},
                 "email": os.environ.get("TEST_PATIENT_EMAIL", "patient1_demo@example.com")
             }
             patient1_ref.set(patient1_data)
             print("환자 'patient1' 데이터 생성/덮어쓰기 완료")

             doctor1_ref = db.collection(PATIENTS_COLLECTION).document('doctor1') # 임시: patients 사용
             # doctor1_ref = db.collection(DOCTORS_COLLECTION).document('doctor1') # 권장
             doctor1_data = {
                 "name": "이지원", "specialty": "내분비내과", "hospital": "서울대병원",
                 "email": os.environ.get("TEST_DOCTOR_EMAIL", "doctor1_demo@example.com")
             }
             doctor1_ref.set(doctor1_data)
             print("의사 'doctor1' 데이터 생성/덮어쓰기 완료")

             # 기존 혈당 데이터 삭제 (선택적)
             # old_glucose_query = db.collection(GLUCOSE_COLLECTION).where('patientId', '==', 'patient1')
             # for doc in old_glucose_query.stream(): doc.reference.delete()
             # print("기존 혈당 데이터 삭제 완료")

             # 샘플 혈당 데이터 생성
             now = datetime.now(timezone.utc)
             batch = db.batch()
             count = 0
             current_glucose = 118
             for i in range(24 * 6): # 최근 12시간 (10분 간격) -> 양 줄임
                 timestamp = now - timedelta(minutes=10 * i)
                 # ... (혈당값 변동 로직은 기존과 유사하게...)
                 current_glucose = max(40, min(300, current_glucose + random.randint(-8, 8)))
                 reading_data = {
                     'patientId': 'patient1', 'value': current_glucose, 'unit': 'mg/dL',
                     'source': 'CGM_Seed', 'timestamp': timestamp
                 }
                 doc_ref = db.collection(GLUCOSE_COLLECTION).document()
                 batch.set(doc_ref, reading_data)
                 count += 1
             batch.commit()
             print(f"샘플 혈당 데이터 {count}개 생성 완료")

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
api.add_resource(SeedDemoData, '/api/seed_demo_data')

# 서버 상태 확인 엔드포인트
@app.route('/api/status', methods=['GET'])
def status():
    webex_status = "Not Configured"
    if webex_token and medical_webex: webex_status = "Connected (Manual Token)" # 토큰 방식 명시
    elif not webex_token and isinstance(medical_webex, MedicalWebexIntegration): webex_status = "Simulation Mode"
    elif webex_token and not medical_webex: webex_status = "Initialization Failed"

    db_status = "Connected" if db else "Disconnected"
    return jsonify({
        "status": "online", "version": "0.5.0-firestore-refactored", # 버전 업데이트
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "webex_status": webex_status, "database_status": db_status
    })

# --- 정적 파일 서빙 라우트 제거 ---
# Vercel에서는 프론트엔드 프로젝트에서 처리하므로 백엔드에서는 제거
# @app.route('/')
# def serve_index(): ...
# @app.route('/static/<path:filename>')
# def serve_static(filename): ...

# --- 메인 실행 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000)) # 포트 번호 변경 가능성 고려 (기존 5371?)
    print(f"Starting server on port {port}...")
    # Vercel 배포 환경 감지하여 디버그 모드 결정
    is_vercel = os.environ.get('VERCEL') == '1'
    app.run(host='0.0.0.0', port=port, debug=not is_vercel)