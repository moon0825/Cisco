import requests
import json
import os
from datetime import datetime
import time

class WebexAPI:
    """
    Cisco Webex API 통합 클래스
    
    이 클래스는 Cisco Webex API를 사용하여 다음 기능을 제공합니다:
    - 인증 및 토큰 관리
    - Webex Instant Connect 세션 생성
    - 메시지 전송
    - 미팅 생성 및 관리
    - 팀 및 공간 관리
    """
    
    def __init__(self, access_token=None, client_id=None, client_secret=None):
        """
        Webex API 클라이언트 초기화
        
        Args:
            access_token: Webex API 액세스 토큰 (선택적)
            client_id: OAuth 클라이언트 ID (선택적)
            client_secret: OAuth 클라이언트 시크릿 (선택적)
        """
        self.base_url = "https://webexapis.com/v1"
        self.access_token = access_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_expiry = None
        
        # 환경 변수에서 토큰 로드 (개발 환경용)
        if not self.access_token and "WEBEX_ACCESS_TOKEN" in os.environ:
            self.access_token = os.environ["WEBEX_ACCESS_TOKEN"]
    
    def _get_headers(self):
        """
        API 요청에 필요한 헤더 생성
        
        Returns:
            headers: 요청 헤더 딕셔너리
        """
        if not self.access_token:
            raise ValueError("액세스 토큰이 설정되지 않았습니다.")
        
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
    
    def _make_request(self, method, endpoint, data=None, params=None, files=None):
        """
        Webex API 요청 수행
        
        Args:
            method: HTTP 메서드 (GET, POST, PUT, DELETE)
            endpoint: API 엔드포인트 경로
            data: 요청 바디 데이터 (선택적)
            params: 쿼리 파라미터 (선택적)
            files: 파일 업로드 (선택적)
            
        Returns:
            response: API 응답 데이터
        """
        url = f"{self.base_url}/{endpoint}"
        headers = self._get_headers()
        
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, params=params)
            elif method == "POST":
                if files:
                    # 파일 업로드 시 Content-Type 헤더 제거
                    headers.pop("Content-Type", None)
                    response = requests.post(url, headers=headers, data=data, files=files)
                else:
                    response = requests.post(url, headers=headers, json=data, params=params)
            elif method == "PUT":
                response = requests.put(url, headers=headers, json=data)
            elif method == "DELETE":
                response = requests.delete(url, headers=headers)
            else:
                raise ValueError(f"지원되지 않는 HTTP 메서드: {method}")
            
            response.raise_for_status()
            
            if response.status_code == 204:  # No Content
                return {"status": "success"}
            
            return response.json()
        
        except requests.exceptions.RequestException as e:
            print(f"API 요청 오류: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"응답 상태 코드: {e.response.status_code}")
                print(f"응답 내용: {e.response.text}")
            raise
    
    def get_user_info(self):
        """
        현재 인증된 사용자 정보 조회
        
        Returns:
            user_info: 사용자 정보
        """
        return self._make_request("GET", "people/me")
    
    # Instant Connect 관련 메서드
    
    def create_instant_connect_session(self, destination_type, destination_address, subject=None):
        """
        Webex Instant Connect 세션 생성
        
        Args:
            destination_type: 대상 유형 ('email', 'phoneNumber', 'roomId' 등)
            destination_address: 대상 주소 (이메일, 전화번호, 룸 ID 등)
            subject: 세션 제목 (선택적)
            
        Returns:
            session_info: 생성된 세션 정보
        """
        data = {
            "destinationType": destination_type,
            "destinationAddress": destination_address
        }
        
        if subject:
            data["subject"] = subject
        
        return self._make_request("POST", "instantconnect/sessions", data=data)
    
    def get_instant_connect_session(self, session_id):
        """
        Instant Connect 세션 정보 조회
        
        Args:
            session_id: 세션 ID
            
        Returns:
            session_info: 세션 정보
        """
        return self._make_request("GET", f"instantconnect/sessions/{session_id}")
    
    def end_instant_connect_session(self, session_id):
        """
        Instant Connect 세션 종료
        
        Args:
            session_id: 세션 ID
            
        Returns:
            result: 종료 결과
        """
        return self._make_request("DELETE", f"instantconnect/sessions/{session_id}")
    
    # 메시지 관련 메서드
    
    def send_message(self, room_id=None, person_email=None, person_id=None, text=None, markdown=None, files=None):
        """
        Webex 메시지 전송
        
        Args:
            room_id: 메시지를 보낼 룸 ID (선택적)
            person_email: 메시지를 보낼 사용자 이메일 (선택적)
            person_id: 메시지를 보낼 사용자 ID (선택적)
            text: 일반 텍스트 메시지 (선택적)
            markdown: 마크다운 형식 메시지 (선택적)
            files: 첨부 파일 URL 리스트 (선택적)
            
        Returns:
            message_info: 전송된 메시지 정보
        """
        data = {}
        
        # 대상 지정 (room_id, person_email, person_id 중 하나는 필수)
        if room_id:
            data["roomId"] = room_id
        elif person_email:
            data["toPersonEmail"] = person_email
        elif person_id:
            data["toPersonId"] = person_id
        else:
            raise ValueError("room_id, person_email, person_id 중 하나는 필수입니다.")
        
        # 메시지 내용 (text, markdown 중 하나는 필수)
        if text:
            data["text"] = text
        elif markdown:
            data["markdown"] = markdown
        else:
            raise ValueError("text 또는 markdown은 필수입니다.")
        
        # 첨부 파일
        if files:
            data["files"] = files
        
        return self._make_request("POST", "messages", data=data)
    
    def get_messages(self, room_id, max_items=50):
        """
        룸의 메시지 목록 조회
        
        Args:
            room_id: 룸 ID
            max_items: 최대 항목 수 (선택적)
            
        Returns:
            messages: 메시지 목록
        """
        params = {
            "roomId": room_id,
            "max": max_items
        }
        
        return self._make_request("GET", "messages", params=params)
    
    # 미팅 관련 메서드
    
    def create_meeting(self, title, start_time, end_time, invitees=None, agenda=None):
        """
        Webex 미팅 생성
        
        Args:
            title: 미팅 제목
            start_time: 시작 시간 (ISO 8601 형식)
            end_time: 종료 시간 (ISO 8601 형식)
            invitees: 초대할 사용자 이메일 리스트 (선택적)
            agenda: 미팅 안건 (선택적)
            
        Returns:
            meeting_info: 생성된 미팅 정보
        """
        data = {
            "title": title,
            "start": start_time,
            "end": end_time
        }
        
        if invitees:
            data["invitees"] = [{"email": email} for email in invitees]
        
        if agenda:
            data["agenda"] = agenda
        
        return self._make_request("POST", "meetings", data=data)
    
    def get_meeting(self, meeting_id):
        """
        미팅 정보 조회
        
        Args:
            meeting_id: 미팅 ID
            
        Returns:
            meeting_info: 미팅 정보
        """
        return self._make_request("GET", f"meetings/{meeting_id}")
    
    def update_meeting(self, meeting_id, title=None, start_time=None, end_time=None, invitees=None, agenda=None):
        """
        미팅 정보 업데이트
        
        Args:
            meeting_id: 미팅 ID
            title: 미팅 제목 (선택적)
            start_time: 시작 시간 (ISO 8601 형식) (선택적)
            end_time: 종료 시간 (ISO 8601 형식) (선택적)
            invitees: 초대할 사용자 이메일 리스트 (선택적)
            agenda: 미팅 안건 (선택적)
            
        Returns:
            meeting_info: 업데이트된 미팅 정보
        """
        data = {}
        
        if title:
            data["title"] = title
        
        if start_time:
            data["start"] = start_time
        
        if end_time:
            data["end"] = end_time
        
        if invitees:
            data["invitees"] = [{"email": email} for email in invitees]
        
        if agenda:
            data["agenda"] = agenda
        
        return self._make_request("PUT", f"meetings/{meeting_id}", data=data)
    
    def delete_meeting(self, meeting_id):
        """
        미팅 삭제
        
        Args:
            meeting_id: 미팅 ID
            
        Returns:
            result: 삭제 결과
        """
        return self._make_request("DELETE", f"meetings/{meeting_id}")
    
    # 팀 및 공간 관련 메서드
    
    def create_team(self, name, description=None):
        """
        Webex 팀 생성
        
        Args:
            name: 팀 이름
            description: 팀 설명 (선택적)
            
        Returns:
            team_info: 생성된 팀 정보
        """
        data = {
            "name": name
        }
        
        if description:
            data["description"] = description
        
        return self._make_request("POST", "teams", data=data)
    
    def create_room(self, title, team_id=None):
        """
        Webex 룸(공간) 생성
        
        Args:
            title: 룸 제목
            team_id: 팀 ID (선택적, 팀에 속한 룸 생성 시)
            
        Returns:
            room_info: 생성된 룸 정보
        """
        data = {
            "title": title
        }
        
        if team_id:
            data["teamId"] = team_id
        
        return self._make_request("POST", "rooms", data=data)
    
    def add_member_to_room(self, room_id, person_email=None, person_id=None, is_moderator=False):
        """
        룸에 멤버 추가
        
        Args:
            room_id: 룸 ID
            person_email: 추가할 사용자 이메일 (선택적)
            person_id: 추가할 사용자 ID (선택적)
            is_moderator: 모더레이터 권한 부여 여부 (선택적)
            
        Returns:
            membership_info: 생성된 멤버십 정보
        """
        data = {
            "roomId": room_id,
            "isModerator": is_moderator
        }
        
        if person_email:
            data["personEmail"] = person_email
        elif person_id:
            data["personId"] = person_id
        else:
            raise ValueError("person_email 또는 person_id는 필수입니다.")
        
        return self._make_request("POST", "memberships", data=data)

# 의료 환경에 특화된 Webex 통합 클래스
class MedicalWebexIntegration:
    """
    의료 환경에 특화된 Webex 통합 클래스
    
    이 클래스는 WebexAPI를 확장하여 의료 환경에 특화된 기능을 제공합니다:
    - 긴급 원격 진료 세션 생성
    - 의료진 팀 협업 공간 관리
    - 환자 알림 및 메시지 전송
    - 정기 원격 진료 일정 관리
    """
    
    def __init__(self, webex_api):
        """
        의료 Webex 통합 초기화
        
        Args:
            webex_api: WebexAPI 인스턴스
        """
        self.webex_api = webex_api
        self.emergency_team_id = None
        self.emergency_room_id = None
    
    def setup_emergency_team(self, team_name="의료 긴급 대응팀", description="1형 당뇨 환자 긴급 대응을 위한 의료진 팀"):
        """
        긴급 대응 의료진 팀 설정
        
        Args:
            team_name: 팀 이름
            description: 팀 설명
            
        Returns:
            team_info: 생성된 팀 정보
        """
        team_info = self.webex_api.create_team(team_name, description)
        self.emergency_team_id = team_info["id"]
        
        # 긴급 대응 룸 생성
        room_info = self.webex_api.create_room("긴급 대응 공간", self.emergency_team_id)
        self.emergency_room_id = room_info["id"]
        
        return team_info
    
    def add_healthcare_provider(self, email, name=None, role=None):
        """
        의료진 추가
        
        Args:
            email: 의료진 이메일
            name: 의료진 이름 (선택적)
            role: 의료진 역할 (선택적)
            
        Returns:
            membership_info: 생성된 멤버십 정보
        """
        if not self.emergency_room_id:
            raise ValueError("긴급 대응 팀이 설정되지 않았습니다. setup_emergency_team()을 먼저 호출하세요.")
        
        membership_info = self.webex_api.add_member_to_room(self.emergency_room_id, person_email=email)
        
        # 환영 메시지 전송
        welcome_message = f"안녕하세요"
        if name:
            welcome_message += f" {name}"
        welcome_message += "님, 1형 당뇨 환자 긴급 대응 팀에 오신 것을 환영합니다."
        
        if role:
            welcome_message += f" 귀하는 {role} 역할로 등록되었습니다."
        
        self.webex_api.send_message(room_id=self.emergency_room_id, text=welcome_message)
        
        return membership_info
    
    def create_emergency_session(self, patient_email, patient_name, glucose_value, prediction, doctor_email=None):
        """
        긴급 원격 진료 세션 생성
        
        Args:
            patient_email: 환자 이메일
            patient_name: 환자 이름
            glucose_value: 현재 혈당 수치
            prediction: 예측된 혈당 수치
            doctor_email: 담당 의사 이메일 (선택적)
            
        Returns:
            session_info: 생성된 세션 정보
        """
        # 세션 제목 생성
        subject = f"긴급 원격 진료: {patient_name} - 혈당 {glucose_value}mg/dL (예측: {prediction}mg/dL)"
        
        # Instant Connect 세션 생성
        session_info = self.webex_api.create_instant_connect_session(
            destination_type="email",
            destination_address=patient_email,
            subject=subject
        )
        
        # 긴급 대응 룸에 알림
        if self.emergency_room_id:
            alert_message = f"⚠️ 긴급 알림: {patient_name} 환자의 혈당이 위험 수준입니다.\n"
            alert_message += f"현재 혈당: {glucose_value}mg/dL\n"
            alert_message += f"예측된 혈당: {prediction}mg/dL\n"
            alert_message += f"긴급 원격 진료 세션이 시작되었습니다.\n"
            alert_message += f"세션 링크: {session_info.get('joinUrl', '링크 없음')}"
            
            self.webex_api.send_message(room_id=self.emergency_room_id, markdown=alert_message)
        
        # 담당 의사에게 직접 메시지 전송
        if doctor_email:
            doctor_message = f"⚠️ 긴급 알림: 귀하의 환자 {patient_name}의 혈당이 위험 수준입니다.\n"
            doctor_message += f"현재 혈당: {glucose_value}mg/dL\n"
            doctor_message += f"예측된 혈당: {prediction}mg/dL\n"
            doctor_message += f"긴급 원격 진료 세션에 참여해 주세요.\n"
            doctor_message += f"세션 링크: {session_info.get('joinUrl', '링크 없음')}"
            
            self.webex_api.send_message(person_email=doctor_email, markdown=doctor_message)
        
        return session_info
    
    def schedule_regular_checkup(self, patient_email, patient_name, doctor_email, doctor_name, 
                                start_time, duration_minutes=30, notes=None):
        """
        정기 원격 진료 일정 예약
        
        Args:
            patient_email: 환자 이메일
            patient_name: 환자 이름
            doctor_email: 의사 이메일
            doctor_name: 의사 이름
            start_time: 시작 시간 (ISO 8601 형식)
            duration_minutes: 진료 시간 (분) (선택적)
            notes: 진료 메모 (선택적)
            
        Returns:
            meeting_info: 생성된 미팅 정보
        """
        # 미팅 제목 생성
        title = f"정기 원격 진료: {doctor_name} 의사 - {patient_name} 환자"
        
        # 시작 및 종료 시간 계산
        start_datetime = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        end_datetime = start_datetime + timedelta(minutes=duration_minutes)
        
        # ISO 8601 형식으로 변환
        start_iso = start_datetime.isoformat().replace('+00:00', 'Z')
        end_iso = end_datetime.isoformat().replace('+00:00', 'Z')
        
        # 미팅 안건 생성
        agenda = f"1형 당뇨 환자 {patient_name}의 정기 원격 진료"
        if notes:
            agenda += f"\n\n메모: {notes}"
        
        # 미팅 생성
        meeting_info = self.webex_api.create_meeting(
            title=title,
            start_time=start_iso,
            end_time=end_iso,
            invitees=[patient_email, doctor_email],
            agenda=agenda
        )
        
        return meeting_info
    
    def send_glucose_alert(self, patient_email, patient_name, glucose_value, prediction, 
                          alert_type="warning", recommendation=None):
        """
        혈당 알림 전송
        
        Args:
            patient_email: 환자 이메일
            patient_name: 환자
(Content truncated due to size limit. Use line ranges to read in chunks)