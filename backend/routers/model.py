from fastapi import FastAPI, HTTPException, BackgroundTasks, APIRouter, Depends
import joblib
import os
import logging
import pandas as pd
import json
from routers.analysis import get_soil_data
from models.user import User
from models.response.pred import PredResponse
from utils.auth import get_current_active_user
from pydantic import BaseModel
from transformers import pipeline, AutoModelForCausalLM, AutoTokenizer  # Import the pipeline from transformers

router = APIRouter()

# Paths
MODEL_PATH = "./ml/soil_model.pkl"
SCALER_PATH = "./ml/scaler.pkl"
ENCODER_PATH = './ml/encoder.pkl'

# Load Model & Scaler
if all(os.path.exists(p) for p in [MODEL_PATH, SCALER_PATH, ENCODER_PATH]):
    model = joblib.load(MODEL_PATH)
    scaler = joblib.load(SCALER_PATH)
    encoder = joblib.load(ENCODER_PATH)  # Load the LabelEncoder
    logging.info("Model artifacts loaded successfully")
else:
    model = scaler = encoder = None
    logging.error("Model artifacts not found! Train the model first.")

DEFAULT_DEPTHS = ["0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm"]
DEFAULT_VALUES = ["Q0.5", "mean"]
REQUIRED_FEATURES = ["bdod", "cec", "cfvo", "clay", "nitrogen", "ocd", "ocs", 
                    "phh2o", "sand", "silt", "soc", "wv0010", "wv0033", "wv1500"]

# Load fine-tuned GPT-2 model
model_path = "../gpt2-soil-advisor"
tokenizer = AutoTokenizer.from_pretrained(model_path)
llm_model = AutoModelForCausalLM.from_pretrained(model_path)
llm_pipeline = pipeline("text-generation", model=llm_model, tokenizer=tokenizer)

def generate_llm_insights(prediction: str, confidence: float, recommendation: str, soil_data: dict) -> str:
    """Generate structured soil insights using fine-tuned GPT-2 model."""
    try:
        # Extract key soil properties with meaningful depth values
        relevant_layers = {"phh2o", "clay", "sand", "soc", "nitrogen", "cec"}  # Key soil properties
        soil_summary = []

        for layer in soil_data.get('data', {}).get('properties', {}).get('layers', []):
            if layer["name"] in relevant_layers:
                depths = layer.get("depths", [])[:2]  # Focus on topsoil layers
                for depth in depths:
                    value_q50 = depth['values'].get('Q0.5', 'N/A')
                    value_mean = depth['values'].get('mean', 'N/A')
                    if value_q50 != 'N/A' and value_mean != 'N/A':
                        soil_summary.append(
                            f"- {layer['name']} ({depth['label']}): Median {value_q50:.2f}, "
                            f"Mean {value_mean:.2f}"
                        )

        soil_summary_text = "\n".join(soil_summary) if soil_summary else "No relevant soil data available."

        # Enhanced prompt for fine-tuned model
        prompt = (
            f"Provide actionable insights on soil conditions and crop recommendations:\n\n"
            f"**Condition**: {prediction}\n"
            f"**Confidence**: {confidence:.2f}\n"
            f"**Recommendation**: {recommendation}\n\n"
            f"**Soil Data**:\n{soil_summary_text}\n\n"
            f"### Insights Required:\n"
            f"1. **Soil Properties**: Identify key properties affecting soil health and suggest improvements.\n"
            f"2. **Crop Suitability**: Recommend specific crops that thrive in the current soil condition.\n"
            f"3. **Environmental Impact**: Assess how soil conditions affect the local ecosystem and suggest mitigation strategies.\n"
            f"4. **Management Practices**: Provide detailed practices to enhance soil quality and crop yield.\n"
            f"5. **Water Management**: Offer strategies to optimize water usage and improve irrigation efficiency.\n"
            f"### Begin your detailed response below:\n\n"
        )

        # Generate response with fine-tuned model
        response = llm_pipeline(
            prompt,
            max_new_tokens=800,
            num_return_sequences=1,
            temperature=0.7,
            top_p=0.9,
            do_sample=True,
            pad_token_id=tokenizer.pad_token_id
        )

        # Process response
        if response and isinstance(response, list) and len(response) > 0:
            generated_text = response[0].get('generated_text', '')

            # Extract response content after the prompt
            split_marker = "### Begin your detailed response below:"
            if split_marker in generated_text:
                cleaned_response = generated_text.split(split_marker)[-1].strip()
            else:
                cleaned_response = generated_text.strip()

            return cleaned_response if cleaned_response else "Error: Response was empty."

        return "Error: No valid response generated."

    except Exception as e:
        logging.error(f"LLM generation error: {str(e)}")
        return f"Error: {str(e)}"

    
@router.get("/predict", response_model=PredResponse,     
            summary="Predict soil condition and recommend crops",
            description="""
            Predicts soil condition based on location (latitude and longitude) using a machine learning model. 
            Returns the predicted condition, confidence score, crop recommendations, and detailed soil data.

            **Inputs:**
            - Longitude (`lon`) and Latitude (`lat`).

            **Outputs:**
            - Predicted condition (e.g., Acidic, Neutral).
            - Confidence score (0-1).
            - Crop recommendations based on condition.
            - Fetched soil data for the location.

            **Error Handling:**
            - Returns an error if the model or soil data is unavailable.
            """)
async def predict_soil(lon: float, 
                       lat: float, 
                       current_user: User = Depends(get_current_active_user)
                       ):
    """Predicts soil condition & provides crop recommendations using real soil data."""
    if model is None or scaler is None:
        raise HTTPException(status_code=500, detail="⚠️ Model not found! Train it first using `train_model.py`.")

    soil_data = await get_soil_data(lon, lat, REQUIRED_FEATURES, DEFAULT_DEPTHS, DEFAULT_VALUES)
    if "data" not in soil_data:
        raise HTTPException(status_code=500, detail="Failed to fetch soil data.")

    try:
        # Extract feature values from soil data
        feature_values = {}
        layers = soil_data.get("data", {}).get("properties", {}).get("layers", [])
        if not isinstance(layers, list):
            raise ValueError("Unexpected structure for layers in soil data.")

        for layer in layers:
            prop_name = layer.get("name")
            if prop_name not in REQUIRED_FEATURES:
                continue

            for depth in layer.get("depths", []):
                depth_label = depth.get("label", "").replace("cm", "").replace("-", "_")
                for value_type, value in depth.get("values", {}).items():
                    key = f"{prop_name}_{depth_label}_{value_type}"
                    feature_values[key] = float(value) if value else 0.0

        # Ensure the feature values are in the correct order expected by the scaler
        if hasattr(scaler, "feature_names_in_"):
            feature_names = scaler.feature_names_in_
            feature_values_ordered = [feature_values.get(feat, 0.0) for feat in feature_names]
        else:
            feature_values_ordered = list(feature_values.values())

        # Convert to DataFrame to match scaler's expected input format
        feature_df = pd.DataFrame([feature_values_ordered], columns=feature_names if hasattr(scaler, "feature_names_in_") else feature_values.keys())
        scaled_data = scaler.transform(feature_df)

        # Make predictions with the updated model
        prediction = encoder.inverse_transform([model.predict(scaled_data)[0]])[0]
        probabilities = model.predict_proba(scaled_data)
        confidence = max(probabilities[0])

        # Updated recommendations based on actual soil types
        recommendations = {
            'Acidic_Organic': "Recommended: Blueberries, Potatoes, Rhododendrons (thrive in acidic organic soils)",
            'Neutral_HighFertility': "Recommended: Wheat, Corn, Soybeans (utilize high nutrient availability)",
            'Sandy_LowFertility': "Recommended: Carrots, Radishes, Lavender (tolerate sandy/low-fertility soils)",
            'Clayey_PoorDrainage': "Recommended: Willows, Rice, Cattails (adapt to heavy clay/poor drainage)",
            'Calcareous': "Recommended: Grapes, Olives, Alfalfa (suitable for alkaline/calcareous soils)",
            'Other': "Recommended: Legumes, Cover Crops (improve soil health)"
        }

        # Generate additional insights using the LLM
        llm_insights = generate_llm_insights(prediction, confidence, recommendations.get(prediction, "Consult an agronomist for custom advice"), soil_data)

        return {
            "condition": prediction,
            "confidence": float(confidence),
            "recommendation": recommendations.get(prediction, "Consult an agronomist for custom advice"),
            "soil_data": soil_data,
            "llm_insights": llm_insights  # Add LLM insights to the response
        }

    except Exception as e:
        logging.error(f"Prediction error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))