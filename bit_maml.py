import numpy as np
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.preprocessing import MinMaxScaler
from sklearn.model_selection import train_test_split
import json
import os
import datetime

# BiT-MAML 모델 클래스 정의
class BiTMAML:
    def __init__(self, input_shape=(12, 1), prediction_horizon=6, meta_learning_rate=0.01):
        """
        BiT-MAML 모델 초기화
        
        Args:
            input_shape: 입력 시퀀스 형태 (시간 단계, 특성 수)
            prediction_horizon: 예측 시간 단계 수 (30분 단위로 6은 3시간을 의미)
            meta_learning_rate: 메타 학습률
        """
        self.input_shape = input_shape
        self.prediction_horizon = prediction_horizon
        self.meta_learning_rate = meta_learning_rate
        self.model = self._build_model()
        self.meta_model = self._build_meta_model()
        self.scaler = MinMaxScaler(feature_range=(0, 1))
        
    def _build_model(self):
        """
        BiT-MAML 모델 아키텍처 구축
        Bidirectional LSTM과 Transformer를 결합한 하이브리드 모델
        """
        # 입력 레이어
        inputs = keras.Input(shape=self.input_shape)
        
        # Bidirectional LSTM 레이어
        x = layers.Bidirectional(layers.LSTM(64, return_sequences=True))(inputs)
        x = layers.Dropout(0.2)(x)
        
        # Transformer 인코더 블록
        # 위치 인코딩 추가
        pos_encoding = self._positional_encoding(self.input_shape[0], 128)
        x = layers.Dense(128)(x)  # 차원 맞추기
        x = x + pos_encoding
        
        # 멀티헤드 어텐션
        attn_output = layers.MultiHeadAttention(
            num_heads=4, key_dim=32
        )(x, x)
        x = layers.LayerNormalization(epsilon=1e-6)(x + attn_output)
        
        # 피드포워드 네트워크
        ffn_output = layers.Dense(256, activation="relu")(x)
        ffn_output = layers.Dense(128)(ffn_output)
        x = layers.LayerNormalization(epsilon=1e-6)(x + ffn_output)
        
        # 글로벌 컨텍스트 추출
        x = layers.GlobalAveragePooling1D()(x)
        
        # 출력 레이어
        outputs = layers.Dense(self.prediction_horizon)(x)
        
        return keras.Model(inputs=inputs, outputs=outputs)
    
    def _build_meta_model(self):
        """
        메타 학습을 위한 모델 구축
        """
        # 기본 모델과 동일한 아키텍처 사용
        return self._build_model()
    
    def _positional_encoding(self, length, depth):
        """
        Transformer를 위한 위치 인코딩 생성
        """
        positions = np.arange(length)[:, np.newaxis]
        depths = np.arange(depth)[np.newaxis, :]/depth
        
        angle_rates = 1 / (10000**depths)
        angle_rads = positions * angle_rates
        
        pos_encoding = np.concatenate(
            [np.sin(angle_rads), np.cos(angle_rads)],
            axis=-1
        )
        
        return tf.cast(pos_encoding[np.newaxis, ...], dtype=tf.float32)
    
    def preprocess_data(self, data):
        """
        데이터 전처리
        
        Args:
            data: 혈당 데이터 시리즈
            
        Returns:
            X: 입력 시퀀스
            y: 타겟 시퀀스
        """
        # 데이터 정규화
        scaled_data = self.scaler.fit_transform(data.reshape(-1, 1))
        
        X, y = [], []
        
        # 시퀀스 생성
        for i in range(len(scaled_data) - self.input_shape[0] - self.prediction_horizon + 1):
            X.append(scaled_data[i:i+self.input_shape[0]])
            y.append(scaled_data[i+self.input_shape[0]:i+self.input_shape[0]+self.prediction_horizon, 0])
        
        return np.array(X), np.array(y)
    
    def meta_train(self, patients_data, epochs=50, inner_steps=5):
        """
        여러 환자 데이터로 메타 학습 수행
        
        Args:
            patients_data: 여러 환자의 혈당 데이터 딕셔너리
            epochs: 메타 학습 에포크 수
            inner_steps: 내부 적응 단계 수
        """
        meta_optimizer = keras.optimizers.Adam(learning_rate=0.001)
        
        for epoch in range(epochs):
            meta_loss = 0
            
            for patient_id, data in patients_data.items():
                # 환자별 데이터 전처리
                X, y = self.preprocess_data(data)
                X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.2)
                
                # 메타 모델 가중치 복사
                self.model.set_weights(self.meta_model.get_weights())
                
                # 내부 적응 단계 (환자별 적응)
                for _ in range(inner_steps):
                    with tf.GradientTape() as tape:
                        predictions = self.model(X_train)
                        loss = keras.losses.mean_squared_error(y_train, predictions)
                    
                    # 그래디언트 계산 및 적용
                    gradients = tape.gradient(loss, self.model.trainable_variables)
                    for i, (var, grad) in enumerate(zip(self.model.trainable_variables, gradients)):
                        self.model.trainable_variables[i].assign_sub(
                            self.meta_learning_rate * grad
                        )
                
                # 검증 손실 계산
                val_predictions = self.model(X_val)
                val_loss = keras.losses.mean_squared_error(y_val, val_predictions)
                meta_loss += val_loss
            
            # 메타 모델 업데이트
            meta_loss /= len(patients_data)
            print(f"Epoch {epoch+1}/{epochs}, Meta Loss: {meta_loss:.4f}")
    
    def adapt_to_patient(self, patient_data, steps=10):
        """
        특정 환자 데이터에 모델 적응
        
        Args:
            patient_data: 환자의 혈당 데이터
            steps: 적응 단계 수
        """
        # 메타 모델 가중치 복사
        self.model.set_weights(self.meta_model.get_weights())
        
        # 데이터 전처리
        X, y = self.preprocess_data(patient_data)
        
        # 환자별 적응
        for step in range(steps):
            with tf.GradientTape() as tape:
                predictions = self.model(X)
                loss = keras.losses.mean_squared_error(y, predictions)
            
            # 그래디언트 계산 및 적용
            gradients = tape.gradient(loss, self.model.trainable_variables)
            for i, (var, grad) in enumerate(zip(self.model.trainable_variables, gradients)):
                self.model.trainable_variables[i].assign_sub(
                    self.meta_learning_rate * grad
                )
            
            print(f"Adaptation Step {step+1}/{steps}, Loss: {loss:.4f}")
    
    def predict(self, sequence):
        """
        혈당 수치 예측
        
        Args:
            sequence: 입력 시퀀스 (최근 혈당 데이터)
            
        Returns:
            predictions: 예측된 혈당 수치
        """
        # 데이터 정규화
        scaled_sequence = self.scaler.transform(sequence.reshape(-1, 1))
        
        # 예측
        scaled_predictions = self.model.predict(scaled_sequence[np.newaxis, :, :])
        
        # 역정규화
        predictions = self.scaler.inverse_transform(scaled_predictions.reshape(-1, 1))
        
        return predictions.flatten()
    
    def save_model(self, path):
        """
        모델 저장
        """
        self.meta_model.save(os.path.join(path, 'meta_model.h5'))
        self.model.save(os.path.join(path, 'adapted_model.h5'))
        
        # 스케일러 저장
        scaler_params = {
            'scale_': self.scaler.scale_.tolist(),
            'min_': self.scaler.min_.tolist(),
            'data_min_': self.scaler.data_min_.tolist(),
            'data_max_': self.scaler.data_max_.tolist(),
            'data_range_': self.scaler.data_range_.tolist()
        }
        
        with open(os.path.join(path, 'scaler.json'), 'w') as f:
            json.dump(scaler_params, f)
    
    def load_model(self, path):
        """
        모델 로드
        """
        self.meta_model = keras.models.load_model(os.path.join(path, 'meta_model.h5'))
        self.model = keras.models.load_model(os.path.join(path, 'adapted_model.h5'))
        
        # 스케일러 로드
        with open(os.path.join(path, 'scaler.json'), 'r') as f:
            scaler_params = json.load(f)
        
        self.scaler.scale_ = np.array(scaler_params['scale_'])
        self.scaler.min_ = np.array(scaler_params['min_'])
        self.scaler.data_min_ = np.array(scaler_params['data_min_'])
        self.scaler.data_max_ = np.array(scaler_params['data_max_'])
        self.scaler.data_range_ = np.array(scaler_params['data_range_'])

# 모델 학습 및 평가를 위한 함수
def generate_synthetic_data(num_patients=5, days=7, interval_minutes=5):
    """
    합성 혈당 데이터 생성
    
    Args:
        num_patients: 환자 수
        days: 일 수
        interval_minutes: 측정 간격 (분)
        
    Returns:
        patients_data: 환자별 혈당 데이터 딕셔너리
    """
    patients_data = {}
    
    # 하루 측정 횟수
    measurements_per_day = 24 * 60 // interval_minutes
    total_measurements = days * measurements_per_day
    
    for patient_id in range(num_patients):
        # 기본 혈당 패턴 (환자별로 다름)
        base_glucose = 120 + patient_id * 10
        amplitude = 30 + patient_id * 5
        
        # 시간 시리즈 생성
        time_points = np.arange(total_measurements)
        
        # 일별 패턴 (24시간 주기)
        daily_pattern = amplitude * np.sin(2 * np.pi * time_points / measurements_per_day)
        
        # 식사 효과 추가
        meal_times = [7, 12, 18]  # 아침, 점심, 저녁 시간 (시간)
        meal_effect = np.zeros(total_measurements)
        
        for day in range(days):
            for meal_time in meal_times:
                meal_index = day * measurements_per_day + (meal_time * 60) // interval_minutes
                if meal_index < total_measurements:
                    # 식사 후 혈당 상승 및 감소 패턴
                    for i in range(60 // interval_minutes):  # 식사 후 1시간 동안
                        if meal_index + i < total_measurements:
                            meal_effect[meal_index + i] += 50 * np.exp(-i / (20 / interval_minutes))
        
        # 무작위성 추가
        noise = np.random.normal(0, 10, total_measurements)
        
        # 최종 혈당 데이터
        glucose_data = base_glucose + daily_pattern + meal_effect + noise
        
        # 현실적인 범위로 제한 (40-400 mg/dL)
        glucose_data = np.clip(glucose_data, 40, 400)
        
        patients_data[f"patient_{patient_id}"] = glucose_data
    
    return patients_data

def train_and_evaluate_model():
    """
    모델 학습 및 평가
    """
    # 합성 데이터 생성
    print("합성 데이터 생성 중...")
    patients_data = generate_synthetic_data()
    
    # 모델 초기화
    print("BiT-MAML 모델 초기화 중...")
    model = BiTMAML(input_shape=(12, 1), prediction_horizon=6)
    
    # 메타 학습
    print("메타 학습 시작...")
    model.meta_train(patients_data, epochs=10, inner_steps=3)
    
    # 테스트 환자 데이터에 적응
    test_patient_id = "patient_0"
    print(f"{test_patient_id}에 모델 적응 중...")
    model.adapt_to_patient(patients_data[test_patient_id], steps=5)
    
    # 예측 테스트
    test_sequence = patients_data[test_patient_id][-12:]
    predictions = model.predict(test_sequence)
    
    print("예측 결과:")
    print(predictions)
    
    # 모델 저장
    os.makedirs("model_weights", exist_ok=True)
    model.save_model("model_weights")
    print("모델이 'model_weights' 디렉토리에 저장되었습니다.")
    
    return model

# 실시간 예측을 위한 함수
def predict_glucose(model, recent_readings, prediction_horizon=6):
    """
    최근 혈당 데이터를 기반으로 미래 혈당 예측
    
    Args:
        model: 학습된 BiT-MAML 모델
        recent_readings: 최근 혈당 측정값 리스트 (최소 12개)
        prediction_horizon: 예측 시간 단계 수
        
    Returns:
        predictions: 예측된 혈당 수치
    """
    # 입력 시퀀스 준비
    if len(recent_readings) < 12:
        raise ValueError("최소 12개의 최근 혈당 측정값이 필요합니다.")
    
    sequence = np.array(recent_readings[-12:])
    
    # 예측
    predictions = model.predict(sequence)
    
    return predictions

if __name__ == "__main__":
    # 모델 학습 및 평가
    model = train_and_evaluate_model()
    
    # 예측 테스트
    test_readings = [120, 125, 130, 140, 150, 160, 155, 150, 145, 140, 135, 130]
    predictions = predict_glucose(model, test_readings)
    
    print("테스트 예측 결과:")
    print(predictions)
