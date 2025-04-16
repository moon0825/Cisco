from flask import Flask, request, jsonify
from flask_restful import Api, Resource
from flask_cors import CORS
import os
import json
import time
from datetime import datetime, timedelta
import random

app = Flask(__name__)
CORS(app)
api = Api(app)

# 임시 데이터 저장소 (실제 구현에서는 데이터베이스 사용)
patients = {}
glucose_readings = {}
predictions = {}
alerts = {}

# 환자 데이터 초기화
def initialize_demo_data():
    # 샘플 환자 데이터
    patients["patient1"] = {
        "id": "patient1",
        "name": "김민수",
        "age": 28,
        "type": "1형 당뇨",
        "diagnosis_date": "2018-05-12",
        "doctor_id": "doctor1",
        "insulin_regimen": "Multiple daily injections",
        "target_glucose_range": {"min": 70, "max": 180}
    }
    
    # 샘플 의사 데이터
    patients["doctor1"] = {
        "id": "doctor1",
        "name": "이지원",
        "specialty": "내분비내과",
        "hospital": "서울대학교병원",
        "patients": ["patient1"]
    }
    
    # 샘플 혈당 데이터 생성 (최근 24시간)
    now = datetime.now()
    glucose_readings["patient1"] = []
    
    # 24시간 동안의 혈당 데이터 생성 (5분 간격)
    for i in range(24 * 12):
        timestamp = now - timedelta(minutes=5 * i)
        
        # 시간대별 혈당 패턴 생성
        hour = timestamp.hour
        if 6 <= hour < 8:  # 아침 식사 전
            base_glucose = 90
        elif 8 <= hour < 10:  # 아침 식사 후
            base_glucose = 160
        elif 11 <= hour < 13:  # 점심 식사 전
            base_glucose = 100
        elif 13 <= hour < 15:  # 점심 식사 후
            base_glucose = 170
        elif 18 <= hour < 20:  # 저녁 식사 전
            base_glucose = 110
        elif 20 <= hour < 22:  # 저녁 식사 후
            base_glucose = 150
        elif 0 <= hour < 4:  # 수면 중
            base_glucose = 85
        else:  # 기타 시간
            base_glucose = 120
        
        # 약간의 무작위성 추가
        glucose_value = max(40, min(300, base_glucose + random.randint(-15, 15)))
        
        glucose_readings["patient1"].append({
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "value": glucose_value,
            "unit": "mg/dL",
            "source": "CGM"
        })

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
            # 쿼리 파라미터로 시간 범위 필터링 지원
            hours = request.args.get('hours', default=24, type=int)
            limit = min(hours * 12, len(glucose_readings[patient_id]))
            
            return {"readings": glucose_readings[patient_id][:limit]}, 200
        return {"error": "Patient not found"}, 404
    
    def post(self, patient_id):
        if patient_id not in patients:
            return {"error": "Patient not found"}, 404
        
        data = request.get_json()
        if not data or 'value' not in data:
            return {"error": "Invalid data format"}, 400
        
        # 새 혈당 데이터 추가
        new_reading = {
            "timestamp": data.get('timestamp', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "value": data['value'],
            "unit": data.get('unit', 'mg/dL'),
            "source": data.get('source', 'Manual')
        }
        
        if patient_id not in glucose_readings:
            glucose_readings[patient_id] = []
        
        glucose_readings[patient_id].insert(0, new_reading)
        
        # 새 데이터 추가 후 예측 업데이트
        self._update_prediction(patient_id)
        
        return {"message": "Glucose reading added", "reading": new_reading}, 201
    
    def _update_prediction(self, patient_id):
        # 실제 구현에서는 BiT-MAML 모델 호출
        # 여기서는 간단한 시뮬레이션
        latest_reading = glucose_readings[patient_id][0]
        current_value = latest_reading['value']
        
        # 30분 후 예측
        prediction_30min = max(40, min(300, current_value + random.randint(-30, 30)))
        # 60분 후 예측
        prediction_60min = max(40, min(300, prediction_30min + random.randint(-20, 20)))
        
        timestamp = datetime.strptime(latest_reading['timestamp'], "%Y-%m-%d %H:%M:%S")
        
        predictions[patient_id] = {
            "current": {
                "timestamp": latest_reading['timestamp'],
                "value": current_value
            },
            "prediction_30min": {
                "timestamp": (timestamp + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                "value": prediction_30min
            },
            "prediction_60min": {
                "timestamp": (timestamp + timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S"),
                "value": prediction_60min
            },
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # 위험 상황 감지 및 알림 생성
        target_range = patients[patient_id]["target_glucose_range"]
        if prediction_30min < target_range["min"]:
            self._create_alert(patient_id, "low", prediction_30min, 30)
        elif prediction_30min > target_range["max"]:
            self._create_alert(patient_id, "high", prediction_30min, 30)
    
    def _create_alert(self, patient_id, alert_type, predicted_value, time_window):
        if patient_id not in alerts:
            alerts[patient_id] = []
        
        new_alert = {
            "id": f"alert_{int(time.time())}",
            "patient_id": patient_id,
            "type": alert_type,
            "predicted_value": predicted_value,
            "time_window": time_window,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "active",
            "acknowledged": False
        }
        
        alerts[patient_id].insert(0, new_alert)
        return new_alert

# 예측 API
class PredictionResource(Resource):
    def get(self, patient_id):
        if patient_id in predictions:
            return predictions[patient_id], 200
        return {"error": "No predictions available for this patient"}, 404

# 알림 API
class AlertResource(Resource):
    def get(self, patient_id):
        if patient_id in alerts:
            # 기본적으로 활성 알림만 반환
            active_only = request.args.get('active_only', default='true', type=str).lower() == 'true'
            
            if active_only:
                active_alerts = [alert for alert in alerts[patient_id] if alert['status'] == 'active']
                return {"alerts": active_alerts}, 200
            else:
                return {"alerts": alerts[patient_id]}, 200
        return {"alerts": []}, 200
    
    def put(self, patient_id, alert_id):
        if patient_id not in alerts:
            return {"error": "Patient not found"}, 404
        
        # 특정 알림 찾기
        alert = next((a for a in alerts[patient_id] if a['id'] == alert_id), None)
        if not alert:
            return {"error": "Alert not found"}, 404
        
        data = request.get_json()
        if not data:
            return {"error": "Invalid data format"}, 400
        
        # 알림 상태 업데이트
        if 'status' in data:
            alert['status'] = data['status']
        
        if 'acknowledged' in data:
            alert['acknowledged'] = data['acknowledged']
        
        return {"message": "Alert updated", "alert": alert}, 200

# Webex 통합 API (시뮬레이션)
class WebexResource(Resource):
    def post(self, action):
        data = request.get_json()
        if not data:
            return {"error": "Invalid data format"}, 400
        
        if action == "instant_connect":
            # Webex Instant Connect 시뮬레이션
            patient_id = data.get('patient_id')
            doctor_id = data.get('doctor_id')
            
            if not patient_id or not doctor_id:
                return {"error": "Patient ID and Doctor ID are required"}, 400
            
            if patient_id not in patients or doctor_id not in patients:
                return {"error": "Patient or Doctor not found"}, 404
            
            # 실제 구현에서는 Webex API 호출
            meeting_info = {
                "meeting_id": f"meeting_{int(time.time())}",
                "patient": patients[patient_id]["name"],
                "doctor": patients[doctor_id]["name"],
                "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "active",
                "join_url": f"https://webex.example.com/meet/{int(time.time())}"
            }
            
            return meeting_info, 201
        
        elif action == "send_message":
            # Webex 메시지 전송 시뮬레이션
            recipient_id = data.get('recipient_id')
            message = data.get('message')
            
            if not recipient_id or not message:
                return {"error": "Recipient ID and message are required"}, 400
            
            if recipient_id not in patients:
                return {"error": "Recipient not found"}, 404
            
            # 실제 구현에서는 Webex API 호출
            message_info = {
                "message_id": f"msg_{int(time.time())}",
                "recipient": patients[recipient_id]["name"],
                "content": message,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "sent"
            }
            
            return message_info, 201
        
        else:
            return {"error": "Invalid action"}, 400

# API 라우트 등록
api.add_resource(PatientResource, '/api/patients/<string:patient_id>')
api.add_resource(GlucoseResource, '/api/patients/<string:patient_id>/glucose')
api.add_resource(PredictionResource, '/api/patients/<string:patient_id>/predictions')
api.add_resource(AlertResource, '/api/patients/<string:patient_id>/alerts', '/api/patients/<string:patient_id>/alerts/<string:alert_id>')
api.add_resource(WebexResource, '/api/webex/<string:action>')

# 서버 상태 확인 엔드포인트
@app.route('/api/status', methods=['GET'])
def status():
    return jsonify({
        "status": "online",
        "version": "0.1.0",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

if __name__ == '__main__':
    # 데모 데이터 초기화
    initialize_demo_data()
    
    # 서버 실행
    app.run(host='0.0.0.0', port=5000, debug=True)
