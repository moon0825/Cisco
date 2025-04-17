# -*- coding: utf-8 -*-
import sqlite3
import json
import os
import time
import random
import uuid
from threading import Thread
from datetime import datetime, timedelta, timezone
import pytz # 시간대 처리

from flask import Flask, request, jsonify, redirect, session, url_for, send_from_directory
from flask_restful import Api, Resource
from flask_cors import CORS
import numpy as np

# --- 로컬 모듈 임포트 (bit_maml.py가 SQLite를 사용하도록 수정되었다고 가정) ---
# bit_maml.py 파일이 로컬 데이터(SQLite)를 사용하도록 수정되어야 합니다.
try:
    # from bit_maml import run_prediction_task, predict_and_store_once
    # 백그라운드 예측 실행은 SQLite 수정 후 활성화 고려
    from bit_maml import predict_and_store_once
except ImportError:
    print("경고: bit_maml.py 모듈을 찾을 수 없거나 내부 오류 발생. 예측 기능이 작동하지 않을 수 있습니다.")
    def predict_and_store_once(username, future_steps=50): # 더미 함수
        print(f"[SIM] predict_and_store_once 호출됨 (사용자: {username}) - 실제 예측 불가")
    # def run_prediction_task(): # 더미 함수
    #     print("[SIM] run_prediction_task 시작 - 실제 예측 불가 (무한 루프 방지 위해 실행 안 함)")

# --- 기본 설정 ---
app = Flask(__name__)
# 로컬 실행 시 고정 시크릿 키 사용 가능 (보안 중요도 낮음)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'local-dev-secret-key')

# --- CORS 설정 (로컬 개발 환경용) ---
# 로컬 프론트엔드 주소에 맞게 수정 (예: http://localhost:3000, http://127.0.0.1:xxxx 등)
FRONTEND_URL = "http://localhost:8000" # frontend/index.html 실행 포트 (예시)
DASHBOARD_URL = "http://localhost:8001" # dashboard-frontend/index.html 실행 포트 (예시)
allowed_origins = [FRONTEND_URL, DASHBOARD_URL, "http://localhost:5000", "http://127.0.0.1:5000"]
CORS(app, origins=allowed_origins, supports_credentials=True)
api = Api(app)

# --- SQLite 데이터베이스 설정 ---
DATABASE = 'local_pgluc.db'
KST = pytz.timezone("Asia/Seoul") # 한국 시간대

def get_db():
    """SQLite 데이터베이스 연결 반환"""
    conn = sqlite3.connect(DATABASE)
    # 결과를 딕셔너리처럼 접근 가능하게 설정
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """데이터베이스 테이블 초기화 (존재하지 않을 경우 생성)"""
    print(f"Initializing SQLite database: {DATABASE}")
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        # 환자 테이블
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS patients (
            id TEXT PRIMARY KEY,
            name TEXT,
            doctor_id TEXT,
            email TEXT,
            target_glucose_range TEXT, -- JSON 문자열 (예: '{"min": 70, "max": 180}')
            other_data TEXT -- 추가 정보 JSON 문자열
        )''')
        # 혈당 측정 테이블
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS glucose_readings (
            doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
            patientId TEXT NOT NULL,
            value REAL,
            unit TEXT,
            source TEXT,
            timestamp TEXT NOT NULL UNIQUE, -- ISO 8601 UTC 문자열
            meal REAL DEFAULT 0,
            exercise REAL DEFAULT 0,
            stressors REAL DEFAULT 0.0,
            hypo_event REAL DEFAULT 0.0,
            hour REAL DEFAULT 0.0,
            is_night REAL DEFAULT 0.0,
            is_meal_time REAL DEFAULT 0.0
        )''')
        # 예측 테이블
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS predictions (
            patientId TEXT NOT NULL,
            timestamp TEXT NOT NULL, -- 예측 대상 시점 (ISO 8601 UTC)
            value REAL,
            predicted_at TEXT, -- 예측 생성 시점 (ISO 8601 UTC)
            PRIMARY KEY (patientId, timestamp)
        )''')
        # 알림 테이블
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patientId TEXT NOT NULL,
            type TEXT, -- 'low', 'high' 등
            predicted_value REAL,
            current_value REAL,
            time_window INTEGER, -- 예: 30 (분)
            message TEXT,
            timestamp TEXT NOT NULL, -- 알림 생성 시점 (ISO 8601 UTC)
            current_reading_timestamp TEXT, -- 관련 혈당 측정 시점 (ISO 8601 UTC)
            status TEXT DEFAULT 'active', -- 'active', 'acknowledged', 'resolved' 등
            acknowledged INTEGER DEFAULT 0, -- 0: false, 1: true
            acknowledged_at TEXT
        )''')
        # 상태 기록 테이블
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id TEXT NOT NULL,
            state TEXT, -- 'meal' or 'exercise'
            value REAL,
            time TEXT NOT NULL -- 상태 기록 시점 (ISO 8601 UTC)
        )''')
        # (선택) Webex 토큰 테이블 (로컬에서는 크게 의미 없을 수 있음)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS webex_tokens (
            user_id TEXT PRIMARY KEY,
            access_token TEXT,
            refresh_token TEXT,
            expires_at TEXT -- ISO 8601 UTC 문자열
        )''')
        conn.commit()
        print("Local SQLite database initialized successfully.")
    except sqlite3.Error as e:
        print(f"!!! Database initialization error: {e} !!!")
    finally:
        if conn:
            conn.close()

# --- Helper Functions ---
def format_timestamp_local(iso_utc_string):
    """ISO UTC 문자열을 KST 'YYYY-MM-DD HH:mm:ss' 형식으로 변환"""
    if not iso_utc_string:
        return None
    try:
        dt_utc = datetime.fromisoformat(iso_utc_string.replace('Z', '+00:00'))
        dt_kst = dt_utc.astimezone(KST)
        return dt_kst.strftime('%Y-%m-%d %H:%mm:%ss')
    except (ValueError, TypeError):
        return iso_utc_string # 변환 실패 시 원본 반환

def dict_factory(cursor, row):
    """SQLite 결과를 딕셔너리로 변환 (json 필드 포함)"""
    d = {}
    for idx, col in enumerate(cursor.description):
        col_name = col[0]
        value = row[idx]
        # JSON 문자열 필드 자동 파싱 시도 (예: target_glucose_range, other_data)
        if isinstance(value, str) and value.startswith('{') and value.endswith('}'):
            try:
                d[col_name] = json.loads(value)
            except json.JSONDecodeError:
                d[col_name] = value # 파싱 실패 시 문자열 그대로
        else:
            d[col_name] = value
    return d

# --- API 리소스 정의 (SQLite 사용) ---

class PatientResource(Resource):
    def get(self, patient_id):
        conn = get_db()
        conn.row_factory = dict_factory # JSON 자동 파싱 포함 팩토리 사용
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM patients WHERE id = ?", (patient_id,))
            patient_data = cursor.fetchone()

            if patient_data:
                # 누락 필드 보완 (데이터베이스에 기본값이 없으므로 여기서 처리)
                patient_data.setdefault("name", "이름 없음")
                patient_data.setdefault("target_glucose_range", {"min": 70, "max": 180})
                return patient_data, 200
            else:
                # 환자 정보 없을 때 기본값 또는 404 반환
                # return {"error": "Patient not found"}, 404
                 default_data = {
                    "id": patient_id,
                    "name": f"{patient_id} (로컬)",
                    "target_glucose_range": {"min": 70, "max": 180}
                }
                 return default_data, 200 # 테스트 편의상 기본값 반환

        except sqlite3.Error as e:
            print(f"SQLite Error fetching patient {patient_id}: {e}")
            return {"error": "Database error fetching patient data"}, 500
        finally:
            if conn: conn.close()

    # POST, PUT, DELETE 메서드 필요시 SQLite에 맞게 구현

class StateResource(Resource):
    def get(self, patient_id):
        """상태 기록 조회 (최근 50개)"""
        conn = get_db()
        conn.row_factory = dict_factory
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM states
                WHERE patient_id = ?
                ORDER BY time DESC
                LIMIT 50
            ''', (patient_id,))
            states = cursor.fetchall()
            return {"states": states}, 200
        except sqlite3.Error as e:
            print(f"SQLite Error fetching states for {patient_id}: {e}")
            return {"error": "Database error fetching state data"}, 500
        finally:
            if conn: conn.close()

    def post(self, patient_id):
        """상태 기록 추가 (및 최신 혈당 로그 업데이트)"""
        payload = request.get_json()
        if not payload:
            return {"error": "No input data provided"}, 400

        field_name = None
        value = None
        state_type = None
        if "meal" in payload:
            field_name = "meal"
            value = float(payload["meal"])
            state_type = "meal"
        elif "exercise" in payload:
            field_name = "exercise"
            value = float(payload["exercise"])
            state_type = "exercise"
        else:
            return {"error": "Invalid state type. Only 'meal' or 'exercise' allowed."}, 400

        conn = get_db()
        try:
            cursor = conn.cursor()
            now_utc_iso = datetime.now(timezone.utc).isoformat()

            # --- 1단계: glulog (glucose_readings) 가장 최근 데이터 업데이트 ---
            # 최신 로그 ID 찾기
            cursor.execute('''
                SELECT doc_id FROM glucose_readings
                WHERE patientId = ? ORDER BY timestamp DESC LIMIT 1
            ''', (patient_id,))
            recent_log = cursor.fetchone()

            if not recent_log:
                 conn.rollback() # 롤백 후 에러 반환
                 return {"error": "No existing glucose log found to update"}, 404

            recent_log_id = recent_log['doc_id']
            # 해당 로그 업데이트 (여기서 field_name은 SQL 인젝션 위험 없음)
            cursor.execute(f'''
                UPDATE glucose_readings SET {field_name} = ?
                WHERE doc_id = ?
            ''', (value, recent_log_id))
            print(f"Updated glucose_readings log {recent_log_id} with {field_name}={value}")

            # --- 2단계: state/{patient_id}/{timestamp} 저장 ---
            cursor.execute('''
                INSERT INTO states (patient_id, state, value, time)
                VALUES (?, ?, ?, ?)
            ''', (patient_id, state_type, value, now_utc_iso))

            conn.commit()
            print(f"[StateResource.post] ✅ {field_name}={value} 업데이트 완료 & 상태 기록 저장")

            # 상태 변경 후 예측 업데이트 트리거
            try:
                predict_and_store_once(patient_id)
            except Exception as pred_e:
                print(f"Warning: Failed to trigger prediction after state update: {pred_e}")

            return {"message": f"{field_name} updated & state saved"}, 200

        except sqlite3.Error as e:
            if conn: conn.rollback()
            print(f"[StateResource.post] SQLite Error: {e}")
            return {"error": "Database error updating state"}, 500
        finally:
            if conn: conn.close()


class GlucoseResource(Resource):
    def get(self, patient_id):
        hours = request.args.get('hours', default=24, type=int)
        # 시간 기준으로 필터링 (SQLite는 문자열 비교 가능)
        limit = hours * 12 # 5분 간격 가정, 최대 조회 개수 (성능 고려)
        limit = min(limit, 2000) # 너무 많지 않게 제한

        time_threshold = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        conn = get_db()
        conn.row_factory = dict_factory
        readings_list = []
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM glucose_readings
                WHERE patientId = ? AND timestamp >= ?
                ORDER BY timestamp ASC -- 시간순으로 반환 (오래된 것부터)
                LIMIT ?
            ''', (patient_id, time_threshold, limit))
            readings_list = cursor.fetchall()
            print(f"환자({patient_id}) 혈당 {len(readings_list)}개 조회 완료 (최근 {hours}시간)")
            return {"readings": readings_list}, 200
        except sqlite3.Error as e:
            print(f"SQLite Error fetching glucose for {patient_id}: {e}")
            return {"error": "Database error fetching glucose data"}, 500
        finally:
            if conn: conn.close()

    def post(self, patient_id):
        # 환자 존재 여부 확인 (선택적)
        # conn_check = get_db()
        # patient_exists = conn_check.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone()
        # conn_check.close()
        # if not patient_exists:
        #     return {"error": "Patient not found"}, 404

        data = request.get_json()
        if not data or 'value' not in data: return {"error": "'value' field is required"}, 400

        conn = get_db()
        try:
            cursor = conn.cursor()
            now_utc_iso = datetime.now(timezone.utc).isoformat()
            value = data['value']
            unit = data.get('unit', 'mg/dL')
            source = data.get('source', 'Manual')
            # 추가 필드 (meal, exercise 등)는 기본값 0으로 들어감
            cursor.execute('''
                INSERT INTO glucose_readings (patientId, value, unit, source, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (patient_id, value, unit, source, now_utc_iso))
            new_id = cursor.lastrowid
            conn.commit()
            print(f'혈당 추가: ID={new_id}, 환자={patient_id} at {now_utc_iso}')

            # 예측 및 알림 로직 트리거
            self._trigger_prediction_update(patient_id)

            return {"message": "Glucose reading added successfully", "id": new_id}, 201
        except sqlite3.IntegrityError as e: # UNIQUE constraint 실패 (timestamp)
            if conn: conn.rollback()
            print(f"SQLite Integrity Error adding glucose for {patient_id}: {e}")
            # 동일 타임스탬프 데이터가 이미 존재할 경우 어떻게 처리할지 결정 (예: 무시, 오류 반환)
            return {"error": "Duplicate timestamp for glucose reading"}, 409 # Conflict
        except sqlite3.Error as e:
            if conn: conn.rollback()
            print(f"SQLite Error adding glucose for {patient_id}: {e}")
            return {"error": "Database error adding glucose data"}, 500
        finally:
            if conn: conn.close()

    def _trigger_prediction_update(self, patient_id):
        """로컬 예측/알림 업데이트 트리거"""
        print(f"Triggering local prediction/alert update for: {patient_id}")
        try:
            # 여기서 직접 로직 실행 또는 백그라운드 작업 큐에 넣기
            self._run_prediction_and_alerting_logic_local(patient_id)
        except Exception as e:
            print(f"!!! Error during prediction/alert trigger ({patient_id}): {e} !!!")

    def _run_prediction_and_alerting_logic_local(self, patient_id):
        """SQLite 기반 예측/알림 로직 (시뮬레이션 포함)"""
        print(f"Running local prediction/alert logic for: {patient_id}")
        conn = get_db()
        conn.row_factory = dict_factory # JSON 파싱 팩토리 사용
        try:
            cursor = conn.cursor()

            # 1. 최신 혈당 데이터 가져오기 (예측 입력용)
            cursor.execute('''
                SELECT value, timestamp FROM glucose_readings
                WHERE patientId = ? ORDER BY timestamp DESC LIMIT 12
            ''', (patient_id,))
            readings = cursor.fetchall()

            if not readings or len(readings) < 1:
                print(f"  예측 위한 혈당 데이터 부족 ({len(readings)}개)")
                return

            # readings는 최신순이므로 시간 역순으로 정렬 필요 (오래된 것부터)
            readings.reverse()
            latest_reading = readings[-1]
            latest_reading_value = latest_reading.get('value')
            latest_reading_ts_iso = latest_reading.get('timestamp')

            if latest_reading_value is None or latest_reading_ts_iso is None:
                print("  최신 혈당 값 또는 타임스탬프 누락")
                return

            # --- 실제 예측 모델 호출 (bit_maml.py가 SQLite 사용하도록 수정되었다고 가정) ---
            # try:
            #     # predict_and_store_once 가 예측 결과를 DB에 저장한다고 가정
            #     predict_and_store_once(patient_id)
            #     # 저장된 최신 예측 결과 가져오기 (예: 30분 후)
            #     cursor.execute('''
            #         SELECT value, timestamp FROM predictions
            #         WHERE patientId = ? ORDER BY timestamp ASC LIMIT 1
            #     ''', (patient_id,)) # ASC: 가장 가까운 미래
            #     pred_30min_row = cursor.fetchone()
            #     prediction_30min = pred_30min_row['value'] if pred_30min_row else None
            #     # 60분 후 예측 등 필요시 추가 조회
            #     print(f"  실제 예측 결과 사용: 30min={prediction_30min}")
            # except Exception as model_err:
            #     print(f"  실제 예측 모델 호출 실패: {model_err}, 시뮬레이션 진행")
            #     prediction_30min = max(40, min(300, latest_reading_value + random.randint(-20, 20)))

            # --- 예측 시뮬레이션 (실제 모델 연동 전) ---
            prediction_30min = max(40, min(300, latest_reading_value + random.randint(-20, 20)))
            # prediction_60min = max(40, min(300, prediction_30min + random.randint(-15, 15)))
            print(f"  예측 시뮬레이션 결과: 30min={prediction_30min}")
            # 시뮬레이션 결과도 predictions 테이블에 저장 (선택적)
            pred_ts_30min = (datetime.fromisoformat(latest_reading_ts_iso) + timedelta(minutes=30)).isoformat()
            now_iso = datetime.now(timezone.utc).isoformat()
            cursor.execute('''
                INSERT OR REPLACE INTO predictions (patientId, timestamp, value, predicted_at)
                VALUES (?, ?, ?, ?)
            ''', (patient_id, pred_ts_30min, prediction_30min, now_iso))
            # conn.commit() # 아래 알림 저장 후 한번에 커밋

            # 2. 알림 생성 로직
            cursor.execute("SELECT target_glucose_range, doctor_id FROM patients WHERE id = ?", (patient_id,))
            patient_info = cursor.fetchone()

            if not patient_info:
                print(f"  환자 정보({patient_id}) 없음, 알림 생성 불가")
                return

            doctor_id = patient_info.get("doctor_id")
            target_range = patient_info.get("target_glucose_range", {"min": 70, "max": 180})
            # target_glucose_range가 JSON 문자열이므로 파싱 필요
            if isinstance(target_range, str):
                try: target_range = json.loads(target_range)
                except: target_range = {"min": 70, "max": 180} # 파싱 실패 시 기본값

            alert_type = None
            predicted_value = None
            if prediction_30min is not None:
                if prediction_30min < target_range["min"]:
                    alert_type = "low"
                    predicted_value = prediction_30min
                elif prediction_30min > target_range["max"]:
                    alert_type = "high"
                    predicted_value = prediction_30min

            if alert_type:
                # 최근 10분 내 동일 타입 알림 확인 (중복 방지)
                ten_minutes_ago_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
                cursor.execute('''
                    SELECT 1 FROM alerts
                    WHERE patientId = ? AND type = ? AND timestamp >= ?
                    LIMIT 1
                ''', (patient_id, alert_type, ten_minutes_ago_iso))

                if cursor.fetchone():
                    print(f"  중복 알림 방지 ({alert_type})")
                    return

                alert_message = f"30분 후 {alert_type} 혈당({predicted_value}mg/dL) 예측. 현재: {latest_reading_value}mg/dL"
                now_utc_iso = datetime.now(timezone.utc).isoformat()

                cursor.execute('''
                    INSERT INTO alerts (patientId, type, predicted_value, current_value, time_window, message, timestamp, current_reading_timestamp, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (patient_id, alert_type, predicted_value, latest_reading_value, 30, alert_message, now_utc_iso, latest_reading_ts_iso, 'active'))
                alert_id = cursor.lastrowid
                conn.commit() # 여기서 예측 결과와 알림 모두 커밋
                print(f"  SQLite 알림 저장: ID={alert_id}")

                # Webex 전송 시뮬레이션
                if doctor_id:
                    cursor.execute("SELECT email FROM patients WHERE id = ?", (doctor_id,)) # 의사 이메일 조회 (patients 테이블 사용 가정)
                    doctor_email_row = cursor.fetchone()
                    if doctor_email_row:
                         print(f"  [SIM] Webex 알림 전송 시도 (의사: {doctor_email_row['email']}) - 메시지: {alert_message}")
                    else:
                         print(f"  의사({doctor_id}) 이메일 정보 없음, Webex 시뮬레이션 불가")
                else:
                     print(f"  담당 의사 미지정, Webex 시뮬레이션 불가")

            else:
                print(f"  정상 범위 예측, 알림 생성 안 함")

        except sqlite3.Error as e:
            if conn: conn.rollback()
            print(f"!!! _run_prediction_and_alerting_logic_local 오류 ({patient_id}): {e} !!!")
        except Exception as e: # 모델 예측 오류 등 다른 예외 처리
            if conn: conn.rollback()
            print(f"!!! _run_prediction_and_alerting_logic_local 예상치 못한 오류 ({patient_id}): {e} !!!")
        finally:
            if conn: conn.close()

class PredictionResource(Resource):
    def get(self, patient_id):
        """환자의 최근 예측 결과 조회 (최근 20개)"""
        conn = get_db()
        conn.row_factory = dict_factory
        try:
            cursor = conn.cursor()
            # timestamp는 예측 대상 시점 (미래)
            cursor.execute('''
                SELECT * FROM predictions
                WHERE patientId = ?
                ORDER BY timestamp DESC
                LIMIT 20
            ''', (patient_id,))
            predictions = cursor.fetchall()
            return {"predictions": predictions}, 200
        except sqlite3.Error as e:
            print(f"SQLite Error fetching predictions for {patient_id}: {e}")
            return {"error": "Database error fetching prediction data"}, 500
        finally:
            if conn: conn.close()

class AlertResource(Resource):
    def get(self, patient_id):
        active_only = request.args.get('active_only', default='true', type=str).lower() == 'true'
        limit = request.args.get('limit', default=10, type=int)

        conn = get_db()
        conn.row_factory = dict_factory
        try:
            cursor = conn.cursor()
            base_query = "SELECT * FROM alerts WHERE patientId = ?"
            params = [patient_id]

            if active_only:
                base_query += " AND status = ?"
                params.append('active')

            base_query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor.execute(base_query, tuple(params))
            alerts_list = cursor.fetchall()
            # acknowledged 필드를 boolean으로 변환 (선택적)
            for alert in alerts_list:
                alert['acknowledged'] = bool(alert.get('acknowledged', 0))
            return {"alerts": alerts_list}, 200
        except sqlite3.Error as e:
            print(f"SQLite Error fetching alerts for {patient_id}: {e}")
            return {"error": "Database error fetching alert data"}, 500
        finally:
            if conn: conn.close()

    def put(self, patient_id, alert_id):
        """알림 상태 업데이트 (확인 처리)"""
        data = request.get_json()
        if not data or ('status' not in data and 'acknowledged' not in data):
            return {"error": "Either 'status' or 'acknowledged' field is required"}, 400

        conn = get_db()
        conn.row_factory = dict_factory
        try:
            cursor = conn.cursor()
            # 알림 존재 및 소유권 확인
            cursor.execute("SELECT * FROM alerts WHERE id = ? AND patientId = ?", (alert_id, patient_id))
            alert = cursor.fetchone()
            if not alert:
                return {"error": "Alert not found or permission denied"}, 404

            update_fields = {}
            if 'status' in data:
                update_fields['status'] = data['status']
            # acknowledged 필드는 boolean으로 받아서 0/1로 변환
            if 'acknowledged' in data:
                update_fields['acknowledged'] = 1 if data['acknowledged'] else 0

            if update_fields:
                update_fields['acknowledged_at'] = datetime.now(timezone.utc).isoformat()

                set_clauses = ", ".join([f"{key} = ?" for key in update_fields.keys()])
                params = list(update_fields.values())
                params.append(alert_id)

                cursor.execute(f"UPDATE alerts SET {set_clauses} WHERE id = ?", tuple(params))
                conn.commit()
                print(f"알림 업데이트 완료 (ID: {alert_id}): {update_fields}")

                # 업데이트된 알림 정보 반환
                cursor.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,))
                updated_alert = cursor.fetchone()
                if updated_alert:
                    updated_alert['acknowledged'] = bool(updated_alert['acknowledged']) # boolean 변환
                    return {"message": "Alert updated successfully", "alert": updated_alert}, 200
                else: # 업데이트 후 조회가 안되는 경우 (드뭄)
                     return {"message": "Alert updated successfully, but failed to retrieve updated data"}, 200
            else:
                return {"message": "No fields to update"}, 304 # Not Modified

        except sqlite3.Error as e:
            if conn: conn.rollback()
            print(f"SQLite Error updating alert {alert_id}: {e}")
            return {"error": "Database error updating alert"}, 500
        finally:
            if conn: conn.close()

# --- Webex API 시뮬레이션 리소스 ---

class WebexEmergencyConnect(Resource):
    def post(self):
        data = request.get_json()
        patient_id = data.get('patient_id')
        print(f"[SIM] Webex 긴급 연결 요청 받음 (환자: {patient_id})")
        return {
            "message": "[SIM] Webex 긴급 연결 성공",
            "id": f"sim_session_{int(time.time())}",
            "joinUrl": "https://webex.example.com/simulated_emergency_session"
        }, 201

class WebexScheduleCheckup(Resource):
    def post(self):
        data = request.get_json()
        patient_id = data.get('patient_id')
        start_time = data.get('start_time')
        print(f"[SIM] Webex 정기 검진 예약 요청 받음 (환자: {patient_id}, 시간: {start_time})")
        return {
            "message": "[SIM] Webex 미팅 예약 성공",
            "meeting_id": f"sim_meeting_{int(time.time())}",
            "join_url": "https://webex.example.com/simulated_checkup_meeting"
        }, 201

class UserWebexStatus(Resource):
     def get(self, user_id):
         print(f"[SIM] 사용자({user_id}) Webex 상태 확인 요청 (로컬 모드에서는 항상 연결 안됨)")
         # 로컬 모드에서는 실제 Webex 연결 상태를 알 수 없으므로 기본값 반환
         return {"connected": False, "email": None}, 200

# --- 의료진 대시보드용 API 리소스 (SQLite 사용) ---

class DoctorPatientList(Resource):
    """ 특정 의사에게 할당된 환자 목록 조회 """
    def get(self, doctor_id):
        print(f"의사({doctor_id})의 환자 목록 조회 시도 (SQLite)")
        conn = get_db()
        conn.row_factory = dict_factory
        patients_list = []
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM patients WHERE doctor_id = ?", (doctor_id,))
            patients = cursor.fetchall()

            # 각 환자의 최신 상태 요약 (여기서는 임시 데이터 사용)
            for patient_data in patients:
                 # 실제 최신 혈당, 예측 조회 로직 추가 필요 (glucose_readings, predictions 테이블 쿼리)
                 patient_data['status'] = random.choice(['normal', 'warning', 'danger']) # 임시
                 patient_data['lastGlucose'] = random.randint(60, 180) # 임시
                 patient_data['trend'] = random.choice(['up', 'down', 'stable']) # 임시
                 patient_data['lastUpdate'] = f"{random.randint(1, 59)}분 전" # 임시
                 patients_list.append(patient_data)

            print(f"의사({doctor_id}) 환자 {len(patients_list)}명 조회 완료 (SQLite)")
            return {"patients": patients_list}, 200
        except sqlite3.Error as e:
            print(f"SQLite Error fetching patient list for doctor {doctor_id}: {e}")
            return {"error": "Database error fetching patient list"}, 500
        finally:
            if conn: conn.close()

class DoctorAlertList(Resource):
    """ 특정 의사의 환자들에게 발생한 알림 목록 조회 """
    def get(self):
        doctor_id = request.args.get('doctor_id')
        limit = request.args.get('limit', default=20, type=int)
        active_only = request.args.get('active_only', default='true', type=str).lower() == 'true'

        if not doctor_id: return {"error": "doctor_id query parameter is required"}, 400
        print(f"의사({doctor_id}) 알림 목록 조회 시도 (SQLite, limit={limit}, active_only={active_only})")

        conn = get_db()
        conn.row_factory = dict_factory
        alerts_list = []
        try:
            cursor = conn.cursor()
            # 1. 해당 의사에게 속한 환자 ID 목록 조회
            cursor.execute("SELECT id FROM patients WHERE doctor_id = ?", (doctor_id,))
            patient_rows = cursor.fetchall()
            patient_ids = [row['id'] for row in patient_rows]

            if not patient_ids:
                print(f"의사({doctor_id})에게 할당된 환자 없음")
                return {"alerts": []}, 200

            # 2. 환자 ID 목록을 사용하여 알림 조회
            # SQLite에서 IN 연산자 사용
            placeholders = ','.join('?' * len(patient_ids))
            base_query = f"SELECT a.*, p.name as patientName FROM alerts a JOIN patients p ON a.patientId = p.id WHERE a.patientId IN ({placeholders})"
            params = list(patient_ids)

            if active_only:
                base_query += " AND a.status = ?"
                params.append('active')

            base_query += " ORDER BY a.timestamp DESC LIMIT ?"
            params.append(limit)

            cursor.execute(base_query, tuple(params))
            alerts_list = cursor.fetchall()
            # acknowledged 필드를 boolean으로 변환
            for alert in alerts_list:
                alert['acknowledged'] = bool(alert.get('acknowledged', 0))

            print(f"의사({doctor_id}) 알림 {len(alerts_list)}개 조회 완료 (SQLite)")
            return {"alerts": alerts_list}, 200
        except sqlite3.Error as e:
            print(f"SQLite Error fetching alerts for doctor {doctor_id}: {e}")
            return {"error": "Database error fetching alert list"}, 500
        finally:
            if conn: conn.close()

class SeedDemoData(Resource):
    """로컬 SQLite DB에 데모 데이터 생성"""
    def post(self):
        print("*** 로컬 SQLite 데모 데이터 시딩 시작 ***")
        conn = get_db()
        try:
            cursor = conn.cursor()
            # 데모 환자/의사 ID 결정
            patient_demo_id = "patient_local_1"
            doctor_demo_id = "doctor_local_1"

            # 의사 데이터 (patients 테이블 사용 가정)
            cursor.execute('''
                INSERT OR REPLACE INTO patients (id, name, email, other_data)
                VALUES (?, ?, ?, ?)
            ''', (doctor_demo_id, "이지원(로컬)", f"{doctor_demo_id}@example.local", json.dumps({"specialty": "내분비내과"})))

            # 환자 데이터
            patient_data = {
                "name": "김민수(로컬)", "age": 28, "type": "1형 당뇨",
                "insulin_regimen": "MDI"
            }
            target_range = {"min": 70, "max": 180}
            cursor.execute('''
                INSERT OR REPLACE INTO patients (id, name, doctor_id, email, target_glucose_range, other_data)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (patient_demo_id, patient_data["name"], doctor_demo_id, f"{patient_demo_id}@example.local",
                  json.dumps(target_range), json.dumps(patient_data)))
            print(f"환자 '{patient_demo_id}' 및 의사 '{doctor_demo_id}' 데이터 생성/덮어쓰기 완료")

            # 샘플 혈당 데이터 생성 (기존 데이터 삭제 후 생성)
            cursor.execute("DELETE FROM glucose_readings WHERE patientId = ?", (patient_demo_id,))
            print(f"기존 혈당 데이터 삭제 ({patient_demo_id})")

            now_utc = datetime.now(timezone.utc)
            count = 0
            current_glucose = random.randint(90, 140)
            batch_data = []
            # 최근 12시간 데이터 (5분 간격)
            for i in range(12 * 12):
                 timestamp_utc = now_utc - timedelta(minutes=5 * i)
                 current_glucose = max(40, min(300, current_glucose + random.randint(-5, 5)))
                 # 식사/운동 랜덤 추가 (선택적)
                 meal = random.randint(0, 50) if random.random() < 0.1 else 0
                 exercise = random.randint(0, 30) if random.random() < 0.05 else 0
                 batch_data.append((
                     patient_demo_id, current_glucose, 'mg/dL', 'CGM_Seed',
                     timestamp_utc.isoformat(), meal, exercise
                 ))
                 count += 1

            cursor.executemany('''
                INSERT INTO glucose_readings (patientId, value, unit, source, timestamp, meal, exercise)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', batch_data)
            conn.commit()
            print(f"샘플 혈당 데이터 {count}개 생성 완료")

            # 초기 예측 트리거
            GlucoseResource()._trigger_prediction_update(patient_demo_id)

            return {"message": f"Local demo data seeded successfully for patient '{patient_demo_id}' and doctor '{doctor_demo_id}'!"}, 201

        except sqlite3.Error as e:
            if conn: conn.rollback()
            print(f"!!! 로컬 데모 데이터 시딩 중 오류: {e} !!!")
            return {"error": f"Failed to seed local demo data: {str(e)}"}, 500
        finally:
            if conn: conn.close()


# --- API 라우트 등록 ---
# 환자 관련
api.add_resource(PatientResource, '/api/patients/<string:patient_id>')
api.add_resource(GlucoseResource, '/api/patients/<string:patient_id>/glucose')
api.add_resource(PredictionResource, '/api/patients/<string:patient_id>/predictions')
api.add_resource(AlertResource, '/api/patients/<string:patient_id>/alerts', '/api/patients/<string:patient_id>/alerts/<string:alert_id>')
api.add_resource(StateResource, '/api/patients/<string:patient_id>/states')

# Webex 시뮬레이션
api.add_resource(WebexEmergencyConnect, '/api/webex/emergency_connect')
api.add_resource(WebexScheduleCheckup, '/api/webex/schedule_checkup')
api.add_resource(UserWebexStatus, '/api/users/<string:user_id>/webex_status') # Webex 상태 확인 (시뮬레이션)

# 의료진 대시보드 관련
api.add_resource(DoctorPatientList, '/api/doctors/<string:doctor_id>/patients')
api.add_resource(DoctorAlertList, '/api/alerts') # doctor_id는 쿼리 파라미터로

# 기타
api.add_resource(SeedDemoData, '/api/seed_demo_data') # 로컬 데이터 생성용

# --- 프론트엔드 파일 서빙 (로컬 테스트용) ---
# 정적 파일 경로 설정 (프로젝트 구조에 맞게 조정)
# 예: backend 폴더와 동일한 레벨에 frontend, dashboard-frontend 폴더가 있다고 가정
STATIC_FOLDER_PATIENT = os.path.abspath('../frontend')
STATIC_FOLDER_DASHBOARD = os.path.abspath('../dashboard-frontend')

@app.route('/')
@app.route('/patient') # 환자 앱 접근 경로
def serve_patient_index():
    print(f"Serving patient index.html from {STATIC_FOLDER_PATIENT}")
    if not os.path.exists(os.path.join(STATIC_FOLDER_PATIENT, 'index.html')):
        return "Error: Patient frontend/index.html not found.", 404
    return send_from_directory(STATIC_FOLDER_PATIENT, 'index.html')

@app.route('/patient/static/<path:filename>') # 환자 앱 정적 파일
def serve_patient_static(filename):
    return send_from_directory(os.path.join(STATIC_FOLDER_PATIENT, 'static'), filename)

@app.route('/dashboard') # 대시보드 접근 경로
def serve_dashboard_index():
    print(f"Serving dashboard index.html from {STATIC_FOLDER_DASHBOARD}")
    if not os.path.exists(os.path.join(STATIC_FOLDER_DASHBOARD, 'index.html')):
        return "Error: Dashboard dashboard-frontend/index.html not found.", 404
    return send_from_directory(STATIC_FOLDER_DASHBOARD, 'index.html')

@app.route('/dashboard/static/<path:filename>') # 대시보드 정적 파일
def serve_dashboard_static(filename):
     # 대시보드 프론트엔드에 static 폴더가 있는지 확인 필요
     static_dir = os.path.join(STATIC_FOLDER_DASHBOARD, 'static')
     if not os.path.isdir(static_dir): # static 폴더가 없다면 dashboard-frontend 루트에서 찾기
         static_dir = STATIC_FOLDER_DASHBOARD
     return send_from_directory(static_dir, filename)


# --- 메인 실행 ---
if __name__ == '__main__':
    # 서버 시작 시 데이터베이스 초기화
    init_db()

    # 백그라운드 예측 스레드 시작 (bit_maml.py가 SQLite 지원 시 주석 해제)
    # print("Starting background prediction task...")
    # prediction_thread = Thread(target=run_prediction_task, daemon=True)
    # prediction_thread.start()

    port = int(os.environ.get('PORT', 5000))
    print(f"Starting local Flask server on http://localhost:{port}")
    print(f"Patient App accessible at: http://localhost:{port}/patient")
    print(f"Dashboard App accessible at: http://localhost:{port}/dashboard")
    print(f"API endpoints available under: http://localhost:{port}/api/...")
    # 로컬 실행 시 debug=True 사용 (코드 변경 시 자동 재시작)
    app.run(host='0.0.0.0', port=port, debug=True)