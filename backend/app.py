# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify
from flask_restful import Api, Resource
from flask_cors import CORS
import os
import json
import time
from datetime import datetime, timedelta
import random

# Webex 통합 모듈 임포트
try:
    from backend.webex_integration import WebexAPI, MedicalWebexIntegration
except ImportError:
    print("오류: webex_integration.py 파일을 찾을 수 없습니다.")
    # 시뮬레이션을 위한 더미 클래스 정의 (파일이 없을 경우)
    class WebexAPI:
        def __init__(self, access_token=None): pass
        def get_user_info(self): return {"displayName": "Simulated User"}

    class MedicalWebexIntegration:
        def __init__(self, webex_api): self.webex_api = webex_api
        def create_emergency_session(self, **kwargs):
            print("시뮬레이션: 긴급 세션 생성", kwargs)
            return {
                "id": f"sim_session_{int(time.time())}",
                "subject": kwargs.get("subject", "시뮬레이션 긴급 세션"),
                "joinUrl": f"https://webex.example.com/sim/{int(time.time())}",
                "created": datetime.now().isoformat()
            }
        def send_glucose_alert(self, **kwargs):
            print("시뮬레이션: 혈당 알림 전송", kwargs)
            return {
                "id": f"sim_msg_{int(time.time())}",
                "created": datetime.now().isoformat(),
                "status": "simulated_sent"
            }
        def schedule_regular_checkup(self, **kwargs):
             print("시뮬레이션: 정기 검진 예약", kwargs)
             return {
                "id": f"sim_meeting_{int(time.time())}",
                "title": kwargs.get("title", "시뮬레이션 정기 검진"),
                "webLink": f"https://webex.example.com/sim_meet/{int(time.time())}",
                "created": datetime.now().isoformat()
            }

app = Flask(__name__)
CORS(app) # 모든 도메인에서의 요청 허용 (개발용)
api = Api(app)

# --- Webex 통합 설정 ---
webex_api = None
medical_webex = None
webex_token = os.environ.get("WEBEX_ACCESS_TOKEN")

if not webex_token:
    print("------------------------------------------------------------")
    print("경고: WEBEX_ACCESS_TOKEN 환경 변수가 설정되지 않았습니다.")
    print("Webex 연동 기능이 시뮬레이션 모드로 작동합니다.")
    print("실제 Webex 연동을 위해서는 환경 변수를 설정해주세요.")
    print("예: export WEBEX_ACCESS_TOKEN='Your_Actual_Webex_Token'")
    print("------------------------------------------------------------")
    # 시뮬레이션 모드 설정 (webex_integration.py가 없거나 토큰이 없을 경우)
    webex_api_sim = WebexAPI() # 더미 클래스 사용
    medical_webex = MedicalWebexIntegration(webex_api_sim)
else:
    try:
        webex_api = WebexAPI(access_token=webex_token)
        # API 연결 테스트 (사용자 정보 조회)
        user_info = webex_api.get_user_info()
        print(f"Webex API 연결 성공: 사용자 '{user_info.get('displayName')}'")
        medical_webex = MedicalWebexIntegration(webex_api)
        # Optional: 긴급 대응팀 설정 (서버 시작 시 1회 실행 등 고려)
        # try:
        #     medical_webex.setup_emergency_team()
        #     print("Webex 긴급 대응팀 설정 완료.")
        # except Exception as e:
        #     print(f"Webex 긴급 대응팀 설정 중 오류 발생: {e}")
    except Exception as e:
        print(f"Webex API 초기화 실패: {e}")
        print("Webex 연동 기능이 시뮬레이션 모드로 작동합니다.")
        webex_api_sim = WebexAPI() # 더미 클래스 사용
        medical_webex = MedicalWebexIntegration(webex_api_sim)

# --- 임시 데이터 저장소 (실제 구현에서는 데이터베이스 사용) ---
patients = {}
glucose_readings = {}
predictions = {}
alerts = {}

# --- 환자/의사 데이터 초기화 (Webex 연동 위해 이메일 추가) ---
def initialize_demo_data():
    global patients, glucose_readings, predictions, alerts
    patients = {}
    glucose_readings = {}
    predictions = {}
    alerts = {}

    # 샘플 환자 데이터
    patients["patient1"] = {
        "id": "patient1",
        "name": "김민수",
        "age": 28,
        "type": "1형 당뇨",
        "diagnosis_date": "2018-05-12",
        "doctor_id": "doctor1",
        "insulin_regimen": "Multiple daily injections",
        "target_glucose_range": {"min": 70, "max": 180},
        "email": os.environ.get("TEST_PATIENT_EMAIL", "muns0825@gmail.com") # 테스트용 이메일 (환경 변수 우선)
    }

    # 샘플 의사 데이터
    patients["doctor1"] = {
        "id": "doctor1",
        "name": "이지원",
        "specialty": "내분비내과",
        "hospital": "서울대학교병원",
        "patients": ["patient1"],
        "email": os.environ.get("TEST_DOCTOR_EMAIL", "mnmn0825@naver.com") # 테스트용 이메일 (환경 변수 우선)
    }

    # 샘플 혈당 데이터 생성 (최근 24시간)
    now = datetime.now()
    glucose_readings["patient1"] = []
    current_glucose = 118 # 현재 값 예시
    glucose_readings["patient1"].append({
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "value": current_glucose,
        "unit": "mg/dL",
        "source": "CGM"
    })

    # 과거 데이터 생성
    for i in range(1, 24 * 12): # 5분 간격
        timestamp = now - timedelta(minutes=5 * i)
        # 간단한 혈당 변동 시뮬레이션
        change = random.randint(-5, 5)
        # 식사 시간 근처에서 더 크게 변동
        hour = timestamp.hour
        if hour in [8, 13, 19]: # 식사 후 시간대
             change += random.randint(5, 15)
        elif hour in [7, 12, 18]: # 식사 전 시간대
             change -= random.randint(5, 10)
        elif hour in [2, 3, 4]: # 새벽 저혈당 가능성
             change -= random.randint(1, 5)

        current_glucose += change
        current_glucose = max(40, min(300, current_glucose)) # 범위 제한

        glucose_readings["patient1"].insert(0, { # 시간 순서대로 앞에 추가
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "value": current_glucose,
            "unit": "mg/dL",
            "source": "CGM"
        })

    # 초기 예측 및 알림 생성 (필요 시)
    if "patient1" in glucose_readings and glucose_readings["patient1"]:
        # GlucoseResource의 인스턴스를 임시로 만들어 _update_prediction 호출
        temp_glucose_resource = GlucoseResource()
        temp_glucose_resource._update_prediction("patient1")


# --- API 리소스 정의 ---

# 환자 API
class PatientResource(Resource):
    def get(self, patient_id):
        if patient_id in patients:
            return patients[patient_id], 200
        return {"error": "Patient not found"}, 404

# 혈당 데이터 API
class GlucoseResource(Resource):
    def get(self, patient_id):
        if patient_id in glucose_readings:
            hours = request.args.get('hours', default=24, type=int)
            # 시간 순서대로 반환 (최신 데이터가 마지막)
            sorted_readings = sorted(glucose_readings[patient_id], key=lambda x: x['timestamp'])
            limit = min(hours * 12, len(sorted_readings))
            # 최근 N 시간 데이터 필터링
            now = datetime.now()
            time_threshold = now - timedelta(hours=hours)
            filtered_readings = [r for r in sorted_readings if datetime.strptime(r['timestamp'], "%Y-%m-%d %H:%M:%S") >= time_threshold]

            return {"readings": filtered_readings}, 200
        return {"error": "Patient not found"}, 404

    def post(self, patient_id):
        if patient_id not in patients:
            return {"error": "Patient not found"}, 404

        data = request.get_json()
        if not data or 'value' not in data:
            return {"error": "Invalid data format"}, 400

        new_reading = {
            "timestamp": data.get('timestamp', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "value": data['value'],
            "unit": data.get('unit', 'mg/dL'),
            "source": data.get('source', 'Manual')
        }

        if patient_id not in glucose_readings:
            glucose_readings[patient_id] = []

        glucose_readings[patient_id].append(new_reading) # 최신 데이터를 뒤에 추가

        # 예측 업데이트
        self._update_prediction(patient_id)

        return {"message": "Glucose reading added", "reading": new_reading}, 201

    def _update_prediction(self, patient_id):
        if patient_id not in glucose_readings or not glucose_readings[patient_id]:
             print(f"경고: {patient_id}에 대한 혈당 데이터가 없어 예측을 건너<0xEB><0x81>니다.")
             return

        # 실제 구현에서는 BiT-MAML 모델 호출
        # 여기서는 간단한 시뮬레이션 (최신 데이터 기반)
        # 시간 순서대로 정렬 후 최신 데이터 가져오기
        sorted_readings = sorted(glucose_readings[patient_id], key=lambda x: x['timestamp'])
        latest_reading = sorted_readings[-1]
        current_value = latest_reading['value']

        # 최근 1시간(12개) 데이터가 있다면 추세 반영 시뮬레이션
        trend_change = 0
        if len(sorted_readings) >= 12:
            recent_12 = [r['value'] for r in sorted_readings[-12:]]
            # 간단 선형 회귀 기울기 계산 (예시)
            x = list(range(12))
            y = recent_12
            avg_x = sum(x) / 12
            avg_y = sum(y) / 12
            slope = sum([(x[i] - avg_x) * (y[i] - avg_y) for i in range(12)]) / sum([(xi - avg_x)**2 for xi in x])
            trend_change = slope * 6 # 30분 후 변화량 추정치 (5분 간격 * 6)

        # 30분 후 예측 (추세 + 랜덤성)
        prediction_30min = current_value + trend_change + random.randint(-10, 10)
        prediction_30min = max(40, min(300, prediction_30min)) # 범위 제한
        # 60분 후 예측
        prediction_60min = prediction_30min + (trend_change * 0.8) + random.randint(-15, 15) # 추세 약간 감소
        prediction_60min = max(40, min(300, prediction_60min)) # 범위 제한

        timestamp = datetime.strptime(latest_reading['timestamp'], "%Y-%m-%d %H:%M:%S")

        predictions[patient_id] = {
            "current": {
                "timestamp": latest_reading['timestamp'],
                "value": current_value
            },
            "prediction_30min": {
                "timestamp": (timestamp + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                "value": int(round(prediction_30min)) # 정수로 반올림
            },
            "prediction_60min": {
                "timestamp": (timestamp + timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S"),
                "value": int(round(prediction_60min)) # 정수로 반올림
            },
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        print(f"{patient_id} 예측 업데이트: 현재={current_value}, 30분후={int(round(prediction_30min))}, 60분후={int(round(prediction_60min))}")

        # 위험 상황 감지 및 알림 생성
        if patient_id in patients:
            target_range = patients[patient_id]["target_glucose_range"]
            if prediction_30min < target_range["min"]:
                self._create_alert(patient_id, "low", int(round(prediction_30min)), 30, latest_reading)
            elif prediction_30min > target_range["max"]:
                self._create_alert(patient_id, "high", int(round(prediction_30min)), 30, latest_reading)
        else:
            print(f"경고: {patient_id} 환자 정보가 없어 알림 생성을 건너<0xEB><0x81>니다.")


    def _create_alert(self, patient_id, alert_type, predicted_value, time_window, latest_reading):
        if patient_id not in alerts:
            alerts[patient_id] = []

        alert_id = f"alert_{int(time.time())}"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        new_alert = {
            "id": alert_id,
            "patient_id": patient_id,
            "type": alert_type, # 'low' or 'high'
            "predicted_value": predicted_value,
            "current_value": latest_reading['value'],
            "time_window": time_window, # minutes
            "timestamp": now_str,
            "status": "active",
            "acknowledged": False,
            "message": f"{time_window}분 후 {alert_type} 혈당({predicted_value}mg/dL) 예측. 현재: {latest_reading['value']}mg/dL"
        }

        # 가장 최근 알림이 동일 유형이고 10분 이내라면 생성 방지 (중복 알림 방지)
        if alerts[patient_id]:
             last_alert = alerts[patient_id][0]
             last_alert_time = datetime.strptime(last_alert['timestamp'], "%Y-%m-%d %H:%M:%S")
             if last_alert['type'] == alert_type and (datetime.now() - last_alert_time) < timedelta(minutes=10):
                 print(f"중복 알림 방지: 최근 10분 내 동일 유형({alert_type}) 알림이 존재합니다.")
                 return None # 알림 생성 안함

        alerts[patient_id].insert(0, new_alert) # 최신 알림을 맨 앞에 추가
        print(f"알림 생성됨 ({patient_id}, {alert_type}): {new_alert['message']}")

        # --- Webex 연동: 알림 발생 시 메시지 전송 ---
        if medical_webex:
            try:
                patient_info = patients.get(patient_id)
                doctor_info = patients.get(patient_info.get("doctor_id")) if patient_info else None

                if patient_info and doctor_info:
                    # 환자 및 의사에게 Webex 메시지 전송
                    recommendation = "혈당 관리에 주의하세요."
                    if alert_type == "low":
                        recommendation = "저혈당 위험! 15-20g의 탄수화물을 섭취하세요."
                    elif alert_type == "high":
                        recommendation = "고혈당 위험! 필요시 의사와 상의하세요."

                    # 의사에게 알림
                    medical_webex.send_glucose_alert(
                        recipient_email=doctor_info.get("email"),
                        recipient_name=doctor_info.get("name"),
                        patient_name=patient_info.get("name"),
                        glucose_value=latest_reading['value'],
                        prediction=predicted_value,
                        alert_type=f"{alert_type}_risk", # "low_risk" or "high_risk"
                        recommendation=f"환자({patient_info.get('name')}) {new_alert['message']} {recommendation}",
                        alert_details_url=f"/patient/{patient_id}/dashboard" # 예시 URL
                    )
                    print(f"Webex 알림 전송 완료 (의사: {doctor_info.get('email')})")

                    # 환자에게 알림 (옵션)
                    # medical_webex.send_glucose_alert(...)

                else:
                    print(f"경고: 환자({patient_id}) 또는 의사 정보를 찾을 수 없어 Webex 알림 전송을 건너<0xEB><0x81>니다.")

            except Exception as e:
                print(f"Webex 알림 전송 중 오류 발생 ({patient_id}): {e}")
        else:
            print("Webex 통합 비활성 상태. 알림 메시지 전송을 건너<0xEB><0x81>니다.")

        return new_alert

# 예측 API
class PredictionResource(Resource):
    def get(self, patient_id):
        # 예측값이 없으면 최신 혈당 기반으로 예측 시도
        if patient_id not in predictions:
            temp_glucose_resource = GlucoseResource()
            temp_glucose_resource._update_prediction(patient_id)

        if patient_id in predictions:
            return predictions[patient_id], 200
        return {"error": "No predictions available for this patient"}, 404

# 알림 API
class AlertResource(Resource):
    def get(self, patient_id):
        if patient_id in alerts:
            active_only = request.args.get('active_only', default='true', type=str).lower() == 'true'

            if active_only:
                active_alerts = [alert for alert in alerts[patient_id] if alert['status'] == 'active']
                return {"alerts": active_alerts}, 200
            else:
                return {"alerts": alerts[patient_id]}, 200
        return {"alerts": []}, 200 # 알림 없으면 빈 리스트 반환

    def put(self, patient_id, alert_id):
        if patient_id not in alerts:
            return {"error": "Patient not found"}, 404

        alert = next((a for a in alerts[patient_id] if a['id'] == alert_id), None)
        if not alert:
            return {"error": "Alert not found"}, 404

        data = request.get_json()
        if not data:
            return {"error": "Invalid data format"}, 400

        updated = False
        if 'status' in data:
            alert['status'] = data['status']
            updated = True
        if 'acknowledged' in data:
            alert['acknowledged'] = data['acknowledged']
            updated = True

        if updated:
             print(f"알림 업데이트됨 ({patient_id}/{alert_id}): status={alert.get('status')}, ack={alert.get('acknowledged')}")
             return {"message": "Alert updated", "alert": alert}, 200
        else:
             return {"message": "No changes detected"}, 304


# --- Webex 통합 API 엔드포인트 ---
class WebexEmergencyConnect(Resource):
    def post(self):
        """긴급 원격 진료 연결 요청"""
        if not medical_webex:
            return {"error": "Webex integration is not available."}, 503

        data = request.get_json()
        if not data or 'patient_id' not in data:
            return {"error": "Patient ID is required"}, 400

        patient_id = data.get('patient_id')
        patient_info = patients.get(patient_id)

        if not patient_info:
            return {"error": "Patient not found"}, 404

        doctor_id = patient_info.get("doctor_id")
        doctor_info = patients.get(doctor_id)

        if not doctor_info:
            return {"error": "Doctor not found for this patient"}, 404

        # 현재 혈당 및 예측 정보 가져오기
        current_glucose = "N/A"
        predicted_glucose = "N/A"
        if patient_id in predictions:
            current_glucose = predictions[patient_id].get('current', {}).get('value', 'N/A')
            predicted_glucose = predictions[patient_id].get('prediction_30min', {}).get('value', 'N/A')

        try:
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
        """정기 원격 진료 예약 요청"""
        if not medical_webex:
            return {"error": "Webex integration is not available."}, 503

        data = request.get_json()
        required_fields = ['patient_id', 'start_time']
        if not data or not all(field in data for field in required_fields):
            return {"error": f"Missing required fields: {required_fields}"}, 400

        patient_id = data.get('patient_id')
        start_time_str = data.get('start_time') # ISO 8601 형식 기대 (예: "2025-04-18T10:00:00Z")
        duration = data.get('duration_minutes', 30)
        notes = data.get('notes')

        patient_info = patients.get(patient_id)
        if not patient_info:
            return {"error": "Patient not found"}, 404

        doctor_id = patient_info.get("doctor_id")
        doctor_info = patients.get(doctor_id)
        if not doctor_info:
            return {"error": "Doctor not found for this patient"}, 404

        try:
            # 시간 형식 검증 (간단하게)
            datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))

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
            # 프론트엔드에서 필요한 정보만 반환 (예: joinUrl)
            return {
                "message": "Meeting scheduled successfully",
                "meeting_id": meeting_info.get('id'),
                "title": meeting_info.get('title'),
                "start_time": meeting_info.get('start'),
                "end_time": meeting_info.get('end'),
                "join_url": meeting_info.get('webLink') # Webex 미팅 참여 링크
            }, 201
        except ValueError:
             return {"error": "Invalid start_time format. Use ISO 8601 format (e.g., YYYY-MM-DDTHH:MM:SSZ)."}, 400
        except Exception as e:
            print(f"Webex 정기 검진 예약 실패: {e}")
            return {"error": f"Failed to schedule Webex meeting: {str(e)}"}, 500


# --- API 라우트 등록 ---
api.add_resource(PatientResource, '/api/patients/<string:patient_id>')
api.add_resource(GlucoseResource, '/api/patients/<string:patient_id>/glucose')
api.add_resource(PredictionResource, '/api/patients/<string:patient_id>/predictions')
api.add_resource(AlertResource, '/api/patients/<string:patient_id>/alerts', '/api/patients/<string:patient_id>/alerts/<string:alert_id>')

# Webex 관련 엔드포인트
api.add_resource(WebexEmergencyConnect, '/api/webex/emergency_connect')
api.add_resource(WebexScheduleCheckup, '/api/webex/schedule_checkup')
# 필요시 다른 Webex 엔드포인트 추가 (예: 메시지 전송 API)

# 서버 상태 확인 엔드포인트
@app.route('/api/status', methods=['GET'])
def status():
    webex_status = "Not Configured"
    if webex_token and webex_api and medical_webex:
         webex_status = "Connected"
    elif not webex_token and medical_webex:
         webex_status = "Simulation Mode"
    elif webex_token and not medical_webex:
         webex_status = "Initialization Failed"

    return jsonify({
        "status": "online",
        "version": "0.2.0-webex-integrated",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "webex_status": webex_status
    })

from flask import send_from_directory

@app.route('/')
def serve_index():
    # app.py와 index.html이 같은 폴더에 있다고 가정
    return send_from_directory('.', 'index.html')

if __name__ == '__main__':
    # 데모 데이터 초기화
    print("Initializing demo data...")
    initialize_demo_data()
    print("Demo data initialized.")

    # 서버 실행
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting server on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=True)