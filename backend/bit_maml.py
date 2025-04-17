import torch
import torch.nn as nn
import numpy as np
from firebase_admin import credentials, firestore, initialize_app
import firebase_admin
import time
from datetime import datetime
import math

# Firebase 초기화 (중복 초기화 방지)
cred = credentials.Certificate("ccccssss2-bde41-firebase-adminsdk-fbsvc-9438d30e40.json")
if not firebase_admin._apps:
    initialize_app(cred)
db = firestore.client()


class PositionalEncoding(nn.Module):
    def __init__(self, d_model, max_len=500):
        super().__init__()
        self.d_model = d_model

        # 위치 인코딩 행렬 생성
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))

        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)  # (1, max_len, d_model)

        self.register_buffer('pe', pe)

    def forward(self, x):
        # x는 (batch_size, seq_length, d_model)이어야 함
        if x.dim() != 3:
            raise ValueError(f"Expected 3D input (batch_size, seq_length, d_model), but got {x.shape}")

        seq_len = x.size(1)
        if seq_len > self.pe.size(1):
            raise ValueError(f"입력 시퀀스 길이 {seq_len}가 PositionalEncoding의 max_len {self.pe.size(1)}보다 큽니다.")

        pe_slice = self.pe[:, :seq_len, :]
        return x + pe_slice


class BiLSTMTransformerHybrid(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim, nhead, num_layers, dropout=0.1):
        super(BiLSTMTransformerHybrid, self).__init__()
        self.bilstm = nn.LSTM(
            input_dim,
            hidden_dim // 2,  # 양방향이면 hidden_dim의 절반씩 사용
            batch_first=True,
            bidirectional=True
        )
        self.pos_encoder = PositionalEncoding(hidden_dim)
        # Transformer EncoderLayer (batch_first=True로 입력 형태 일치)
        encoder_layers = nn.TransformerEncoderLayer(
            d_model=hidden_dim,
            nhead=nhead,
            dropout=dropout,
            batch_first=True
        )
        self.transformer_encoder = nn.TransformerEncoder(encoder_layers, num_layers=num_layers)

        self.fc1 = nn.Linear(hidden_dim, hidden_dim // 2)
        self.fc2 = nn.Linear(hidden_dim // 2, output_dim)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        # 원래 입력 차원(2D: (seq_length, input_dim) 또는 3D: (batch_size, seq_length, input_dim)) 기억
        original_dim = x.dim()
        if original_dim == 2:
            x = x.unsqueeze(0)  # (1, seq_length, input_dim)

        if x.dim() != 3:
            raise ValueError(f"Expected 3D input (batch_size, seq_length, input_dim), but got {x.shape}")

        # LSTM 처리 → (batch_size, seq_length, hidden_dim)
        bilstm_out, _ = self.bilstm(x)

        # 위치 인코딩 후 Transformer Encoder 적용
        transformer_input = self.pos_encoder(bilstm_out)
        transformer_output = self.transformer_encoder(transformer_input)

        # Transformer의 전체 시퀀스에 대해 예측 (각 타임스텝별)
        hidden = self.dropout(self.relu(self.fc1(transformer_output)))
        out = self.fc2(hidden)  # (batch_size, seq_length, output_dim)

        # output_dim이 1인 경우 마지막 차원을 squeeze → (batch_size, seq_length)
        if out.shape[-1] == 1:
            out = out.squeeze(-1)

        # 원래 입력이 2D였다면 배치 차원 제거 → (seq_length,)
        if original_dim == 2:
            out = out.squeeze(0)
        return out


# MAMLTrainer 클래스
class MAMLTrainer:
    def __init__(self, model, lr_inner, lr_meta, num_inner_steps):
        self.model = model
        self.lr_inner = lr_inner
        self.lr_meta = lr_meta
        self.num_inner_steps = num_inner_steps
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.lr_meta)

    def inner_update(self, model, support_inputs, support_targets):
        criterion = torch.nn.L1Loss()
        support_inputs = torch.tensor(support_inputs, dtype=torch.float32)
        support_targets = torch.tensor(support_targets, dtype=torch.float32)

        for _ in range(self.num_inner_steps):
            predictions = model(support_inputs)
            loss = criterion(predictions, support_targets)
            model.zero_grad()
            loss.backward()
            for param in model.parameters():
                param.data -= self.lr_inner * param.grad.data
        return model


# 사전학습된 모델 로드 함수
def load_pretrained_model(model_class, filepath, *args, **kwargs):
    model = model_class(*args, **kwargs)
    model.load_state_dict(torch.load(filepath))
    model.eval()
    print(f"Pretrained model loaded from {filepath}")
    return model


# 타임스탬프 문자열을 유닉스 타임스탬프로 변환
def timestamp_to_unix(ts_str):
    return int(datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp())


# 유닉스 타임스탬프를 문자열로 변환
def unix_to_timestamp(unix_ts):
    return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")


# Min-Max 스케일링 함수
def min_max_scale(data, min_val, max_val):
    return (data - min_val) / (max_val - min_val)


# Min-Max 복원 함수
def min_max_unscale(scaled_data, min_val, max_val):
    return scaled_data * (max_val - min_val) + min_val


# 미래 시점 예측 함수
def predict_future(model_trainer, recent_data, future_steps=15):
    timestamps = [x[0] for x in recent_data]
    features = [x[1:] for x in recent_data]
    glucose_values = np.array([x[1] for x in recent_data], dtype=np.float32).reshape(-1, 1)

    # ✅ glucose_level만 scaling (index 0)
    glucose_min, glucose_max = 80, 600
    glucose_scaled = (glucose_values - glucose_min) / (glucose_max - glucose_min)

    # 전체 입력 = scaled glucose + 나머지 그대로
    features_np = np.array(features, dtype=np.float32)
    support_inputs = np.concatenate([glucose_scaled, features_np[:, 1:]], axis=1)

    # ❗ target도 glucose만 사용 (seq_length 만큼)
    support_targets = glucose_scaled

    # Adaptation
    adapted_model = model_trainer.inner_update(model_trainer.model, support_inputs, support_targets)

    # 시간 처리
    time_step = timestamps[-1] - timestamps[-2] if len(timestamps) >= 2 else 300
    last_timestamp = timestamps[-1]
    future_timestamps = [last_timestamp + (i + 1) * time_step for i in range(future_steps)]
    future_timestamps_str = [unix_to_timestamp(ts) for ts in future_timestamps]

    # future input 준비: 최근 step 그대로 유지
    last_input = support_inputs[-1].copy()  # shape: (8,)
    future_inputs = []
    for i in range(future_steps):
        step_input = last_input.copy()

        if i >= 6:
            step_input[1] = 0  # meal
            step_input[2] = 0  # exercise
        if i >= 4:
            step_input[3] = 0  # stressors
        if i >= 3:
            step_input[4] = 0  # hypo_event

        future_inputs.append(step_input)

    future_inputs = torch.tensor(future_inputs, dtype=torch.float32)

    with torch.no_grad():
        predictions = adapted_model(future_inputs).cpu().numpy()

    # ✅ glucose_level만 역변환
    predictions_unscaled = predictions * (glucose_max - glucose_min) + glucose_min

    return list(zip(future_timestamps_str, predictions_unscaled.flatten()))


# Firestore에 예측 결과 저장 (timestamp를 키로 사용)
def save_predictions(username, predictions):
    collection_ref = db.collection(f"users/{username}/predict")
    batch = db.batch()

    for timestamp, value in predictions:
        doc_ref = collection_ref.document(timestamp)
        batch.set(doc_ref, {
            'timestamp': timestamp,
            'value': float(value),
            'predicted_at': datetime.now().isoformat()
        })

    batch.commit()
    print(f"{len(predictions)}개의 예측 데이터를 Firestore의 'users/{username}/predict'에 저장 완료.")


# 데이터 개수 확인 및 예측 루프
def monitor_and_predict(model_trainer, username, target_count=64, check_interval=60, future_steps=15):
    collection_ref = db.collection(f"users/{username}/glulog")

    while True:
        docs = collection_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(target_count).stream()
        recent_data = []
        for doc in docs:
            timestamp_str = doc.id
            try:
                timestamp = timestamp_to_unix(timestamp_str)
                glucose = doc.to_dict().get('glucose')
                if glucose is not None:
                    meal = doc.to_dict().get('meal', 0)
                    exercise = doc.to_dict().get('exercise', 0)
                    stressors = doc.to_dict().get('stressors', 0.0)
                    hypo_event = doc.to_dict().get('hypo_event', 0.0)
                    hour = doc.to_dict().get('hour', 0.0)
                    is_night = doc.to_dict().get('is_night', 0.0)
                    is_meal_time = doc.to_dict().get('is_meal_time', 0.0)
                    recent_data.append(
                        (timestamp, glucose, meal, exercise, stressors, hypo_event, hour, is_night, is_meal_time))
            except ValueError:
                print(f"잘못된 타임스탬프 형식: {timestamp_str}")
                continue

        current_count = len(recent_data)
        print(f"현재 데이터 개수: {current_count}/{target_count}")

        if current_count >= target_count:
            print("✅ 충분한 데이터 확보됨. 예측 수행 중...")
            recent_data.sort(key=lambda x: x[0])
            future_predictions = predict_future(model_trainer, recent_data, future_steps)
            save_predictions(username, future_predictions)
        else:
            print("⚠️ 데이터 부족. 예측 생략.")

        time.sleep(check_interval)  # 1분 대기 # 1분마다 예측


def run_prediction_task():
    pretrained_model = load_pretrained_model(
        BiLSTMTransformerHybrid,
        "rmse_pretrained.pth",
        input_dim=8,
        hidden_dim=128,
        output_dim=1,
        nhead=8,
        num_layers=2,
        dropout=0.1
    )
    trainer = MAMLTrainer(pretrained_model, lr_inner=0.01, lr_meta=0.001, num_inner_steps=30)
    monitor_and_predict(
        trainer,
        username="kimjaehoug",
        target_count=64,
        check_interval=60,  # 🔁 내부적으로 1분마다 반복
        future_steps=20
    )


# bit_maml.py 파일 안에 추가
def predict_and_store_once(username="kimjaehoug", future_steps=15):
    pretrained_model = load_pretrained_model(
        BiLSTMTransformerHybrid,
        "rmse_pretrained.pth",
        input_dim=8,
        hidden_dim=128,
        output_dim=1,
        nhead=8,
        num_layers=2,
        dropout=0.1
    )
    trainer = MAMLTrainer(pretrained_model, lr_inner=0.01, lr_meta=0.001, num_inner_steps=30)

    # 최신 데이터 로드 (예측에 사용할 recent_data 100개)
    collection_ref = db.collection(f"users/{username}/glulog")
    docs = collection_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(100).stream()

    recent_data = []
    for doc in docs:
        data = doc.to_dict()
        ts_str = doc.id
        try:
            timestamp = timestamp_to_unix(ts_str)
            recent_data.append((
                timestamp,
                data.get("glucose", 0),
                data.get("meal", 0),
                data.get("exercise", 0),
                data.get("stressors", 0),
                data.get("hypo_event", 0),
                data.get("hour", 0),
                data.get("is_night", 0),
                data.get("is_meal_time", 0)
            ))
        except:
            continue

    recent_data = sorted(recent_data, key=lambda x: x[0])

    if len(recent_data) >= 64:
        future_predictions = predict_future(trainer, recent_data, future_steps)
        save_predictions(username, future_predictions)
        print(f"[predict_and_store_once] ✅ 예측 완료 및 저장됨 ({len(future_predictions)}개)")
    else:
        print("[predict_and_store_once] ❌ 데이터 부족. 예측 생략.")

# 실행
if __name__ == "__main__":
    # 모델 로드
    pretrained_model = load_pretrained_model(
        BiLSTMTransformerHybrid,
        "rmse_pretrained.pth",
        input_dim=8,
        hidden_dim=128,
        output_dim=1,
        nhead=8,
        num_layers=2,
        dropout=0.1
    )
    trainer = MAMLTrainer(pretrained_model, lr_inner=0.01, lr_meta=0.001, num_inner_steps=30)

    # 사용자 이름 설정
    username = "kimjaehoug"

    # 데이터 모니터링 및 예측 시작
    monitor_and_predict(
        trainer,
        username=username,
        target_count=64,
        check_interval=60,  # 1분
        future_steps=200  # 100개 시점
    )