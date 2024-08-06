import logging
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import requests
from requests.auth import HTTPBasicAuth
from azure.identity import DefaultAzureCredential
from openai import AzureOpenAI
import time
import tempfile
import os 
import base64
import json
from azure.storage.blob import BlobServiceClient, ContentSettings, generate_blob_sas, BlobSasPermissions
from datetime import datetime, timedelta
from azure.data.tables import TableServiceClient, TableEntity
from dotenv import load_dotenv

load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

DEFAULT_MESSAGE_TYPE = 'transcription'

# Configura Azure Speech y OpenAI
azure_speech_key = os.getenv('AZ_SPEECH_KEY')
azure_speech_region = 'eastus'
voice_name = "es-MX-DaliaNeural"

openai_endpoint = 'https://openai-eastus2-models.openai.azure.com/'
openai_api_key = os.getenv('AZ_OPENAI_KEY')
deployment_id = 'gpt-35-turbo'
whisper_deployment_id = 'whisper-1'

# Configuring Azure Storage
azure_storage_connection_string = os.getenv('AZ_CONNECTION_STRING_STORAGE')
azure_container_name = 'temp-container'

# Initializing Azure Storage Tables
logger.info("Init Storage Table client")
azure_table_name = "conversationLogs"
table_service_client = TableServiceClient.from_connection_string(conn_str=azure_storage_connection_string)
table_client = table_service_client.get_table_client(azure_table_name)

# Credenciales de Twilio
twilio_account_sid = os.getenv('TWILIO_SID')
twilio_auth_token = os.getenv('TWILIO_AUTH_TOKEN')

llm_system_prompt = "You are a real estate expert, your job is only to explain the characteristics of a property, if the user shows interest, say that you will contact an advisor as soon as possible, your goal is to advise the user superficially and provide first hand information, do not answer topics of conversation irrelevant to your goal. Respond to the user in the spoken language"

# Configura el cliente de Azure OpenAI
openai_client = AzureOpenAI(
  azure_endpoint = openai_endpoint, 
  api_key=openai_api_key,  
  api_version='2024-06-01',
)

# Configura el cliente de Twilio
twilio_client = Client(twilio_account_sid, twilio_auth_token)

@app.route("/voice", methods=['POST'])
def voice():
    logger.info("Inicio de la llamada")
    # Configura Twilio para capturar la voz del usuario
    resp = VoiceResponse()
    resp.say("Hola, ¿cómo puedo ayudarte?", language="es-mx")
    resp.record(max_length=60, action="/process_voice", timeout=2, play_beep=False)
    return Response(str(resp), mimetype="text/xml")

@app.route("/process_voice", methods=['POST'])
def process_voice():
    # Starting timer for metrics
    start_time = time.time()
    recording_url = request.form['RecordingUrl']
    from_number = request.form["From"]
    twilio_call_ssid = request.form["CallSid"]

    logger.info(f"Grabación recibida: {recording_url}")
    
    # Descargar la grabación de Twilio
    try:
        time.sleep(1)
        audio_data = download_audio(recording_url)
        logger.info("Audio descargado")
    except Exception as e:
        logger.error(f"Error al descargar el audio: {e}")
        return Response("Error al descargar el audio", status=500)

    # Guardar el audio en un archivo temporal
    audio_file_path = "temp_audio.mp3"
    temp_dir = tempfile.mkdtemp()
    temp_file = os.path.join(temp_dir, audio_file_path) 
    with open(temp_file, "wb") as file:
        file.write(audio_data)

    logger.info("Audio data saved in temp file")
    audio_data_temp = open(temp_file, 'rb')
    logger.info("Sending audio data from temp file to STT model")

    #audio_file_path = "temp_audio.mp3"
    #with open(audio_file_path, "wb") as audio_file:
        #audio_file.write(audio_data)

    # Transcribir el audio
    try:
        transcript = transcribe_audio(audio_data_temp)
        logger.info(f"Transcripción: {transcript}")

        # Saving transcription in conversation log
        save_message_in_table("user", transcript, recording_url, from_number, twilio_call_ssid)

    except Exception as e:
        logger.error(f"Error al transcribir el audio: {e}")
        return Response("Error al transcribir el audio", status=500)
    
    # Generar respuesta utilizando OpenAI
    try:
        response_text = generate_response(transcript, recording_url, from_number, twilio_call_ssid)
        logger.info(f"Respuesta generada: {response_text}")

        save_message_in_table("assistant", response_text, recording_url, from_number, twilio_call_ssid)

    except Exception as e:
        logger.error(f"Error al generar la respuesta: {e}")
        return Response("Error al generar la respuesta", status=500)
    
    # Convertir la respuesta en voz
    try:
        audio_response = synthesize_speech(response_text)
        logger.info("Uploading audio response to blob storage")
        current_date_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f+00:00')
        filename = f"{from_number}/audio-{current_date_time}.mp3"
        audio_url = upload_to_blob(audio_response, filename)
    except Exception as e:
        logger.error(f"Error al sintetizar la respuesta o decodificando: {e}")
        return Response("Error al sintetizar la respuesta", status=500)
    
    # Responder con el audio generado
    resp = VoiceResponse()
    resp.say(response_text, language="es-mx")
    resp.record(max_length=60, action="/process_voice", timeout=2, play_beep=False)

    end_time = time.time()
    execution_time = end_time - start_time
    logger.info(f"El tiempo de ejecución del script es: {execution_time} segundos")

    return Response(str(resp), mimetype="text/xml")

def generate_response(transcript, recording_url, from_number, twilio_call_ssid):
    logger.info("Getting conversation log of the contact")
    query_filter = f"messageFromPhone eq '{from_number}'" 
    logger.info(query_filter)
    raw_contact_conversations = [entity for entity in table_client.query_entities(query_filter)]
    logger.info("Sorting conversations")
    sorted_contact_conversations = sorted(raw_contact_conversations, key=lambda x: x.get('messageTime'))
    logger.info("Loading json messageContent for every conversation entity")
    contact_conversations = [json.loads(entity["messageContent"]) for entity in sorted_contact_conversations]
    logger.info(f"Conversation Logs from contact: \n{contact_conversations}")

    default_system_message = {"role": "system", "content": llm_system_prompt}
    initial_conversations = [default_system_message]
    # The numbers of last conversations to use
    last_conversation_count = 5
    conversations = initial_conversations + contact_conversations[-last_conversation_count:] if last_conversation_count else contact_conversations

    response = openai_client.chat.completions.create(
        model=deployment_id,
        messages=conversations,
    )
    return response.choices[0].message.content.strip()

def save_message_in_table(role: str, transcription: str, recording_url, from_number, twilio_call_ssid):
    current_date_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f+00:00')
    logger.info("Saving message in conversationLogs")
    user_message_obj = {"role": role, "content": transcription}
    user_message_json = json.dumps(user_message_obj)
    new_message_entity = {
        "PartitionKey": DEFAULT_MESSAGE_TYPE,
        "RowKey": f"{twilio_call_ssid}-{current_date_time}",
        "messageSource": 'phone',
        "messageTime": current_date_time,
        "messageFromPhone": from_number,
        "messageContent": user_message_json,
    }
    logger.info(f"Saving new message entity in conversationLogs: \n{new_message_entity}")
    table_client.create_entity(new_message_entity)

def download_audio(recording_url):
    # Extraer el SID de la grabación de la URL
    recording_sid = recording_url.split('/')[-1]

    # Intentar descargar la grabación con lógica de reintento
    retries = 6
    for attempt in range(retries):
        try:
            # Obtener la grabación utilizando el SID
            recording = twilio_client.recordings(recording_sid).fetch()
            logger.info(f"Recordings by sid: {recording}")

            # Descargar la grabación
            url = recording.uri.replace('.json', '.mp3')
            full_url = f'https://api.twilio.com{url}'
            response = requests.get(full_url, auth=HTTPBasicAuth(twilio_account_sid, twilio_auth_token))

            if response.status_code == 200:
                logger.info("Audio descargado desde Twilio")
                return response.content
            else:
                logger.error(f"Error al descargar el audio: {response.status_code} {response.text}")
                response.raise_for_status()
        except Exception as e:
            logger.error(f"Intento {attempt + 1} de {retries} fallido: {e}")
            if attempt < retries - 1:
                logger.info("Reintentando en 0.5 segundos...")
                time.sleep(0.5)
            else:
                raise

def transcribe_audio(raw_audio):
    transcript = openai_client.audio.transcriptions.create(
        model=whisper_deployment_id,
        file=raw_audio,
        response_format="text",
        language="es"
    )
    logger.info(f"Transcription result from whisper-1: {transcript}")
    return transcript    


def synthesize_speech(text):
    # URL para obtener el token de autenticación
    token_url = f"https://{azure_speech_region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"

    # URL para la síntesis de voz
    tts_url = f"https://{azure_speech_region}.tts.speech.microsoft.com/cognitiveservices/v1"

    # Solicitar el token de autenticación
    headers = {
        'Ocp-Apim-Subscription-Key': azure_speech_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    response = requests.post(token_url, headers=headers)
    if response.status_code != 200:
        logger.error(f"Error al obtener el token: {response.status_code} - {response.text}")
        raise Exception(f"Error al obtener el token: {response.status_code} - {response.text}")

    access_token = response.text

    # Configurar la solicitud de síntesis de voz
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/ssml+xml',
        'X-Microsoft-OutputFormat': 'riff-16khz-16bit-mono-pcm'
    }
    body = f"""
    <speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='es-MX'>
        <voice name='{voice_name}'>{text}</voice>
    </speak>
    """

    # Solicitar la síntesis de voz
    logger.info(f"Sintetizando texto {text}")
    response = requests.post(tts_url, headers=headers, data=body)
    if response.status_code == 200:
        logger.info("Texto sintetizado en audio")
        return response.content
    else:
        logger.error(f"Error al sintetizar el audio: {response.status_code} - {response.text}")
        raise Exception(f"Error al sintetizar el audio: {response.status_code} - {response.text}")

def upload_to_blob(audio_data, filename):
    blob_service_client = BlobServiceClient.from_connection_string(azure_storage_connection_string)
    blob_client = blob_service_client.get_blob_client(container=azure_container_name, blob=filename)

    logger.info(f"Subiendo archivo a Azure Blob Storage: {filename}")
    content_settings = ContentSettings(content_type='audio/mpeg')
    blob_client.upload_blob(audio_data, overwrite=True, content_settings=content_settings)

    sas_token = generate_blob_sas(
        account_name=blob_client.account_name,
        container_name=blob_client.container_name,
        blob_name=blob_client.blob_name,
        account_key=blob_service_client.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(hours=1)
    )

    blob_url = f"https://{blob_client.account_name}.blob.core.windows.net/{blob_client.container_name}/{blob_client.blob_name}?{sas_token}"
    logger.info(f"Archivo subido. URL: {blob_url}")
    return blob_url

if __name__ == "__main__":
    app.run(debug=True)

