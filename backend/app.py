# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify, redirect, session, url_for # send_from_directory 제거, redirect 등 추가
from flask_restful import Api, Resource
from flask_cors import CORS
import os
import json
import time
from datetime import datetime, timedelta, timezone
import random
import requests # Webex OAuth 토큰 교환용
import uuid     # OAuth state 생성용
import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core import exceptions as google_exceptions
import base64 # 추가
import json   # 추가

# --- Firebase 초기화 (Base64 방식) ---
SERVICE_ACCOUNT_KEY_BASE64 = os.environ.get('FIREBASE_SERVICE_ACCOUNT_BASE64') # 새 환경 변수 이름 사용
db = None

if SERVICE_ACCOUNT_KEY_BASE64:
    try:
        # Base64 디코딩 및 JSON 파싱
        print("Base64 서비스 계정 키 디코딩 시도...")
        key_json_str = base64.b64decode(SERVICE_ACCOUNT_KEY_BASE64).decode('utf-8')
        key_dict = json.loads(key_json_str) # JSON 문자열을 파이썬 딕셔너리로 변환
        print("서비스 계정 키 파싱 성공.")

        # 앱 중복 초기화 방지
        if not firebase_admin._apps:
            # 파일 경로 대신 딕셔너리를 사용하여 초기화
            cred = credentials.Certificate(key_dict)
            firebase_admin.initialize_app(cred)
            print("Firebase Admin SDK 초기화 성공 (Base64)")
        else:
            print("Firebase Admin SDK 이미 초기화됨 (Base64)")

        db = firestore.client()
        print("Firebase Firestore 클라이언트 생성 및 연결 성공")
    except Exception as e:
        print(f"!!! Firebase 초기화 오류 (Base64): {e} !!!")
        db = None
else:
    print("!!! 경고: FIREBASE_SERVICE_ACCOUNT_BASE64 환경 변수가 설정되지 않았습니다. Firestore 기능이 비활성화됩니다. !!!")

# --- Flask 앱 설정 ---
app = Flask(__name__)
# Flask 세션 사용을 위한 시크릿 키 설정 (반드시 환경 변수로 설정!)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-needs-to-be-set-in-env')
if app.secret_key == 'dev-secret-needs-to-be-set-in-env' and os.environ.get('VERCEL') == '1':
     print("!!! 경고: FLASK_SECRET_KEY 환경 변수가 설정되지 않았습니다. 프로덕션 환경에서는 반드시 설정해야 합니다. !!!")

FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://cisco-git-main-kihoon-moons-projects.vercel.app")
CORS(app, origins=[FRONTEND_URL, "http://localhost:5000"], supports_credentials=True)
api = Api(app)

# --- Webex OAuth 설정 ---
WEBEX_CLIENT_ID = os.environ.get('WEBEX_CLIENT_ID')
WEBEX_CLIENT_SECRET = os.environ.get('WEBEX_CLIENT_SECRET')
WEBEX_REDIRECT_URI = os.environ.get('WEBEX_REDIRECT_URI') # 예: https://cisco-ejsn.vercel.app/api/webex/auth/callback
WEBEX_AUTHORIZE_URL = "https://webexapis.com/v1/authorize"
WEBEX_TOKEN_URL = "https://webexapis.com/v1/access_token"
WEBEX_SCOPES = os.environ.get("WEBEX_SCOPES", "spark:messages_write meeting:schedules_write meeting:schedules_read spark:people_read") # 필요한 최소 범위 설정 권장

# --- Webex 통합 모듈 임포트 (기존 유지) ---
try:
    from webex_integration import WebexAPI, MedicalWebexIntegration
except ImportError:
    print("경고: webex_integration.py 모듈을 찾을 수 없습니다. Webex 기능이 시뮬레이션됩니다.")
    # Dummy 클래스 정의 (기존 유지)
    class WebexAPI: pass
    class MedicalWebexIntegration: pass

# --- Firestore 컬렉션 이름 ---
PATIENTS_COLLECTION = 'patients'
GLUCOSE_COLLECTION = 'glucoseReadings'
PREDICTIONS_COLLECTION = 'predictions'
ALERTS_COLLECTION = 'alerts'
TOKENS_COLLECTION = 'webex_tokens' # Webex 토큰 저장용 컬렉션

# --- Helper Function (기존 유지) ---
def firestore_timestamp_to_iso(timestamp):
    # ... (이전 코드와 동일) ...
    if timestamp and hasattr(timestamp, 'isoformat'):
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.isoformat(timespec='seconds')
    return None

# --- Webex Token 관리 함수 (Firestore 사용) ---
def store_tokens(user_id, access_token, refresh_token, expires_in):
    """Firestore에 토큰 저장"""
    if not db: return False
    try:
        # expires_at 계산 시 expires_in이 문자열일 수 있으므로 int 변환 및 예외 처리
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 300) # 5분 여유
        token_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        token_ref.set({
            'user_id': user_id,
            'access_token': access_token,
            'refresh_token': refresh_token,
            'expires_at': expires_at # Firestore는 datetime 객체 저장 가능
        }, merge=True) # merge=True로 기존 필드 유지 가능성 (선택)
        print(f"토큰 저장 완료: 사용자={user_id}")
        return True
    except Exception as e:
        print(f"!!! 토큰 저장 실패 ({user_id}): {e} !!!")
        return False

def get_tokens(user_id):
    """Firestore에서 토큰 가져오기"""
    if not db: return None
    try:
        token_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        doc = token_ref.get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"!!! 토큰 가져오기 실패 ({user_id}): {e} !!!")
        return None

def refresh_tokens(user_id, refresh_token):
    """Refresh Token을 사용하여 새 토큰 발급 및 저장"""
    if not db or not WEBEX_CLIENT_ID or not WEBEX_CLIENT_SECRET:
        print("!!! 토큰 갱신 불가: 설정 부족 !!!")
        return None
    if not refresh_token:
        print(f"!!! 토큰 갱신 불가: Refresh Token 없음 ({user_id}) !!!")
        return None

    print(f"Webex 토큰 갱신 시도: 사용자={user_id}")
    payload = {
        'grant_type': 'refresh_token',
        'client_id': WEBEX_CLIENT_ID,
        'client_secret': WEBEX_CLIENT_SECRET,
        'refresh_token': refresh_token
    }
    try:
        response = requests.post(WEBEX_TOKEN_URL, data=payload)
        response.raise_for_status()
        data = response.json()
        print(f"토큰 갱신 성공: 사용자={user_id}")
        # 새 토큰 저장 (Refresh Token도 갱신될 수 있음)
        store_tokens(user_id, data['access_token'], data.get('refresh_token', refresh_token), data['expires_in'])
        return data['access_token']
    except requests.exceptions.RequestException as e:
        print(f"!!! 토큰 갱신 API 요청 실패 ({user_id}): {e} !!!")
        if e.response is not None and e.response.status_code in [400, 401]: # 잘못된 토큰 등
            print("Refresh Token이 만료되었거나 잘못되었을 수 있습니다. 재인증 필요.")
            try: # Firestore에서 해당 유저 토큰 삭제
                 db.collection(TOKENS_COLLECTION).document(user_id).delete()
                 print(f"갱신 실패로 사용자 {user_id} 토큰 삭제됨")
            except: pass
        return None

def get_valid_webex_token(user_id):
    """유효한 Access Token 가져오기 (필요시 갱신)"""
    token_data = get_tokens(user_id)
    if not token_data: return None # 토큰 없음

    expires_at = token_data.get('expires_at')
    # Firestore Timestamp 객체를 Python datetime 객체로 변환 (이미 datetime이면 그대로 사용)
    if expires_at and not isinstance(expires_at, datetime):
        try:
            # Firestore 타임스탬프는 UTC이므로 timezone.utc 추가
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        except Exception as e:
             print(f"경고: expires_at 변환 실패 ({user_id}): {e}")
             expires_at = None # 변환 실패 시 만료된 것으로 간주

    # 만료 시간 확인 (타임존 인식 비교)
    if expires_at and expires_at > datetime.now(timezone.utc):
        print(f"유효한 토큰 사용: 사용자={user_id}")
        return token_data.get('access_token')
    else:
        print(f"토큰 만료 또는 시간 정보 없음, 갱신 시도: 사용자={user_id}")
        return refresh_tokens(user_id, token_data.get('refresh_token'))

def get_webex_api_client_for_user(user_id):
     """특정 사용자의 유효한 토큰으로 WebexAPI 클라이언트 인스턴스 생성"""
     access_token = get_valid_webex_token(user_id)
     if access_token:
         return WebexAPI(access_token=access_token)
     else:
         return None # 유효 토큰 없음

# --- OAuth 인증 관련 API 엔드포인트 ---
@app.route('/api/webex/auth/initiate')
def webex_auth_initiate():
    # 인증을 시작하는 사용자 ID를 파라미터로 받거나 세션 등에서 가져와야 함
    user_id_to_auth = request.args.get('user_id', 'doctor1') # 데모용 기본값

    if not all([WEBEX_CLIENT_ID, WEBEX_REDIRECT_URI, WEBEX_SCOPES]):
        return jsonify({"error": "Webex OAuth client not configured properly in env vars"}), 500

    state = str(uuid.uuid4())
    session['webex_oauth_state'] = state
    session['webex_auth_user_id'] = user_id_to_auth # 어떤 사용자가 인증하는지 저장

    params = { 'response_type': 'code', 'client_id': WEBEX_CLIENT_ID,
               'redirect_uri': WEBEX_REDIRECT_URI, 'scope': WEBEX_SCOPES, 'state': state }
    auth_url = f"{WEBEX_AUTHORIZE_URL}?{requests.compat.urlencode(params)}"
    print(f"Webex 인증 시작: 사용자={user_id_to_auth}, URL={auth_url}")
    return redirect(auth_url)

@app.route('/api/webex/auth/callback')
def webex_auth_callback():
    error = request.args.get('error')
    if error: return jsonify({"error": "Webex OAuth Error", "details": error}), 400

    code = request.args.get('code')
    state = request.args.get('state')
    expected_state = session.pop('webex_oauth_state', None)
    user_id = session.pop('webex_auth_user_id', None)

    if not expected_state or state != expected_state: return jsonify({"error": "Invalid OAuth state"}), 400
    if not code: return jsonify({"error": "Missing OAuth code"}), 400
    if not user_id: return jsonify({"error": "User context lost"}), 400
    if not all([WEBEX_CLIENT_ID, WEBEX_CLIENT_SECRET, WEBEX_REDIRECT_URI]):
         return jsonify({"error": "Webex OAuth client not configured"}), 500

    payload = { 'grant_type': 'authorization_code', 'client_id': WEBEX_CLIENT_ID,
                'client_secret': WEBEX_CLIENT_SECRET, 'code': code, 'redirect_uri': WEBEX_REDIRECT_URI }
    try:
        response = requests.post(WEBEX_TOKEN_URL, data=payload)
        response.raise_for_status()
        data = response.json()
        success = store_tokens(user_id, data['access_token'], data['refresh_token'], data['expires_in'])
        if success:
             # 인증 성공 후 프론트엔드의 특정 페이지로 리디렉션하거나 성공 메시지 표시
             # return redirect(f"{FRONTEND_URL}/auth/success?user_id={user_id}") # 예시
             return jsonify({"message": f"Webex 인증 성공: 사용자={user_id}"})
        else:
             return jsonify({"error": "토큰 저장 실패"}), 500
    except requests.exceptions.RequestException as e:
        print(f"!!! 토큰 교환 API 요청 실패: {e} !!!")
        # ... (오류 처리 - 기존 코드 유지) ...
        return jsonify({"error": "토큰 교환 실패", "details": str(e)}), 500
    except Exception as e:
         print(f"!!! 토큰 교환/저장 중 오류: {e} !!!")
         return jsonify({"error": "서버 내부 오류 (토큰 교환)"}), 500

# --- API 리소스 정의 (Firestore 및 동적 Webex 클라이언트 사용) ---
# (PatientResource, GlucoseResource 등 Firestore 로직 복원/구현 필요)
# 아래는 Firestore 로직이 복원되었다고 가정한 Webex 호출 부분 예시

class PatientResource(Resource):
    """환자 정보 API (Firestore 사용)"""
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            doc = db.collection(PATIENTS_COLLECTION).document(patient_id).get()
            return (doc.to_dict(), 200) if doc.exists else ({"error": "환자 없음"}, 404)
        except Exception as e:
            print(f"환자({patient_id}) 조회 오류: {e}")
            return {"error": "서버 오류 (환자 조회)"}, 500

class GlucoseResource(Resource):
    """혈당 데이터 API (Firestore 사용)"""
    def get(self, patient_id):
        # 이전 답변의 Firestore get 로직 사용
        if not db: return {"error": "DB 미연결"}, 503
        # ... (Firestore 쿼리 및 결과 반환 로직 구현) ...
        # 예시로 빈 리스트 반환 (반드시 구현 필요)
        print(f"경고: GlucoseResource.get 미구현 - 환자({patient_id})")
        return {"readings": []}, 200

    def post(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        # ... (Firestore에 혈당 데이터 추가 로직 구현) ...
        # 예시로 성공 반환 (반드시 구현 필요)
        print(f"경고: GlucoseResource.post 미구현 - 환자({patient_id})")
        self._trigger_prediction_update(patient_id) # 예측 트리거 호출은 유지
        return {"message": "혈당 추가 성공 (임시)", "id": "temp_id"}, 201

    def _trigger_prediction_update(self, patient_id):
        print(f"예측/알림 업데이트 트리거: {patient_id}")
        try:
            self._run_prediction_and_alerting_logic(patient_id)
        except Exception as e:
            print(f"!!! 예측/알림 로직 실행 중 오류 ({patient_id}): {e} !!!")

    def _run_prediction_and_alerting_logic(self, patient_id):
        """Firestore 데이터 기반 예측/알림 실행 및 Webex 전송"""
        if not db: return
        print(f"Firestore 예측/알림 로직 시작: {patient_id}")
        # ... (Firestore에서 혈당 읽기, 예측 시뮬레이션, 예측 저장 로직 구현 - 이전 답변 참조) ...
        # 예시: 예측 로직 후 알림 발생 가정
        alert_type = "low" # 예시
        predicted_value = 65 # 예시
        latest_reading_value = 75 # 예시

        if alert_type:
            # 알림 저장 및 Webex 전송
            patient_snap = db.collection(PATIENTS_COLLECTION).document(patient_id).get()
            if not patient_snap.exists: return
            patient_info = patient_snap.to_dict()
            doctor_id = patient_info.get("doctor_id")
            if not doctor_id: return

            # **의사(doctor_id)의 토큰으로 Webex 클라이언트 생성**
            webex_api_client = get_webex_api_client(doctor_id)
            if not webex_api_client:
                 print(f"!!! 의사({doctor_id}) Webex 토큰 없음/만료. 알림 전송 불가 !!!")
                 # TODO: 관리자나 다른 채널로 알림 전달 로직 고려
                 return

            medical_webex_instance = MedicalWebexIntegration(webex_api_client)

            # Firestore에 알림 저장 로직 구현...
            # Webex 메시지 전송 로직... (이전 답변 참조, medical_webex_instance 사용)
            print(f"Webex 알림 전송 시도: 의사={doctor_id}, 환자={patient_id}, 유형={alert_type}")
            # medical_webex_instance.send_glucose_alert(...)

class PredictionResource(Resource):
    """예측 정보 API (Firestore 사용)"""
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        # ... (Firestore에서 예측 정보 읽는 로직 구현 - 이전 답변 참조) ...
        print(f"경고: PredictionResource.get 미구현 - 환자({patient_id})")
        return {"error": "아직 예측 정보 없음"}, 404

class AlertResource(Resource):
    """알림 정보 API (Firestore 사용)"""
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        # ... (Firestore에서 알림 정보 읽는 로직 구현 - 이전 답변 참조) ...
        print(f"경고: AlertResource.get 미구현 - 환자({patient_id})")
        return {"alerts": []}, 200

    def put(self, patient_id, alert_id):
        if not db: return {"error": "DB 미연결"}, 503
        # ... (Firestore에서 알림 정보 업데이트 로직 구현 - 이전 답변 참조) ...
        print(f"경고: AlertResource.put 미구현 - 환자({patient_id}), 알림({alert_id})")
        return {"message": "알림 업데이트 성공 (임시)"}, 200

class WebexEmergencyConnect(Resource):
    """긴급 연결 API (OAuth 및 Firestore 사용)"""
    def post(self):
        if not db: return {"error": "DB 미연결"}, 503

        data = request.get_json()
        if not data or 'patient_id' not in data: return {"error": "patient_id 필수"}, 400
        patient_id = data.get('patient_id')
        # ** 중요: 어떤 사용자(의사)가 이 요청을 했는지 알아야 함 **
        # 여기서는 임시로 요청 본문에 'requesting_user_id'가 있다고 가정 (프론트에서 보내줘야 함)
        requesting_user_id = data.get('requesting_user_id', 'doctor1') # 실제로는 로그인 세션 등에서 가져와야 함

        # 요청한 사용자의 유효한 토큰으로 Webex 클라이언트 생성
        webex_api_client = get_webex_api_client(requesting_user_id)
        if not webex_api_client:
            # 401 Unauthorized: 클라이언트에게 재인증이 필요함을 알림
            return {"error": "Webex 인증 필요", "reauth_url": url_for('webex_auth_initiate', user_id=requesting_user_id, _external=True)}, 401

        medical_webex_instance = MedicalWebexIntegration(webex_api_client)

        try:
            # Firestore에서 환자/담당의사 정보 조회 (이전 답변 참조)
            patient_snap = db.collection(PATIENTS_COLLECTION).document(patient_id).get()
            if not patient_snap.exists: return {"error": "환자 없음"}, 404
            patient_info = patient_snap.to_dict()
            doctor_id = patient_info.get("doctor_id")
            if not doctor_id: return {"error": "담당 의사 미지정"}, 400
            doctor_snap = db.collection(PATIENTS_COLLECTION).document(doctor_id).get() # 임시
            if not doctor_snap.exists: return {"error": f"의사({doctor_id}) 없음"}, 404
            doctor_info = doctor_snap.to_dict()
            # Firestore에서 예측 정보 조회 (이전 답변 참조)
            pred_snap = db.collection(PREDICTIONS_COLLECTION).document(patient_id).get()
            current_glucose = pred_snap.to_dict().get('current',{}).get('value','N/A') if pred_snap.exists else 'N/A'
            predicted_glucose = pred_snap.to_dict().get('prediction_30min',{}).get('value','N/A') if pred_snap.exists else 'N/A'

            print(f"Webex 긴급 연결 시도 (사용자 {requesting_user_id}): 환자={patient_info.get('name')}, 의사={doctor_info.get('name')}")
            session_info = medical_webex_instance.create_emergency_session(
                patient_email=patient_info.get("email"), patient_name=patient_info.get("name"),
                glucose_value=current_glucose, prediction=predicted_glucose,
                doctor_email=doctor_info.get("email")
            )
            print(f"Webex 긴급 연결 성공: 세션 ID={session_info.get('id')}")
            return session_info, 201
        except Exception as e:
            print(f"!!! Webex 긴급 연결 실패: {e} !!!")
            # TODO: 여기서도 401 에러 처리 (토큰 만료/갱신 실패) -> 재인증 유도
            return {"error": f"Webex 긴급 연결 실패: {str(e)}"}, 500


class WebexScheduleCheckup(Resource):
    """정기 검진 예약 API (OAuth 및 Firestore 사용)"""
    def post(self):
        # WebexEmergencyConnect 와 유사하게 구현 필요
        print("경고: WebexScheduleCheckup.post 미구현")
        return {"error": "Not Implemented Yet"}, 501


class SeedDemoData(Resource):
    """Firestore 데모 데이터 생성 API"""
    def post(self):
        if not db: return {"error": "DB 미연결"}, 503
        # ... (이전 답변의 Firestore 데이터 생성 로직 구현) ...
        print("경고: SeedDemoData.post 미구현")
        return {"message": "Demo data seeding successful! (임시)"}, 201


# --- API 라우트 등록 (기존 유지) ---
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
    webex_config_status = "Configured" if all([WEBEX_CLIENT_ID, WEBEX_CLIENT_SECRET, WEBEX_REDIRECT_URI]) else "Not Configured"
    db_status = "Connected" if db else "Disconnected"
    return jsonify({
        "status": "online", "version": "0.6.0-oauth-scaffold", # 버전 업데이트
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "webex_config_status": webex_config_status,
        "database_status": db_status
    })

# --- 정적 파일 서빙 라우트 제거 ---

# --- 메인 실행 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting server on port {port}...")
    is_vercel = os.environ.get('VERCEL') == '1'
    app.run(host='0.0.0.0', port=port, debug=not is_vercel)