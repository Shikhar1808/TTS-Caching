import azure.cognitiveservices.speech as speechsdk
import os, time
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()


class AzureTTS:
    def getAudio(self, fileName, text):
        subscription_key = os.getenv("AZURE_SPEECH_KEY")
        region = os.getenv("AZURE_SPEECH_REGION", "centralindia")

        if not subscription_key:
            raise ValueError("AZURE_SPEECH_KEY is not set in the environment or .env file.")

        speech_config = speechsdk.SpeechConfig(subscription=subscription_key, region=region)

        speech_config.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Riff8Khz16BitMonoPcm)

        # speech_config.speech_synthesis_language = "mr-IN"   # -- Marathi
        # speech_config.speech_synthesis_voice_name = "mr-IN-AarohiNeural"    # -- Marathi

        # speech_config.speech_synthesis_language = "hi-IN"   # -- Hindi
        # speech_config.speech_synthesis_voice_name = "hi-IN-KavyaNeural"    # -- Hindi

        speech_config.speech_synthesis_language = "en-IN"   # -- English
        speech_config.speech_synthesis_voice_name = "en-IN-KavyaNeural"    # -- English

        # speech_config.speech_synthesis_language = "mr-IN"   # -- Marathi
        # speech_config.speech_synthesis_voice_name = "mr-IN-AarohiNeural"    # -- Marathi

        audio_config = speechsdk.audio.AudioOutputConfig(filename=fileName)

        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
        # synthesizer.speak_text_async(text)

        result = synthesizer.speak_text_async(text).get()

        # Check the result for any errors
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            print(f"Audio content written to file: {fileName}")
        else:
            print(f"Speech synthesis failed: {result.reason}")



if __name__ == "__main__":
    ttsObj = AzureTTS()
    jsonObj = {
            "P_wrong_pin":"Unfortunately, we are unable to assist you right now, but our team will contact you soon",
            "P1": "Hello! I am your virtual assistant from Faeber India. To begin, may I please know your preferred language - Hindi, or English?",
            "P4_negative":"We apologize that your details are wrong. Don’t worry, I will edit your details first",
            "P15":"May I please know the Date of Purchase of the product? If you dont remember the exact date, you may also share the nearest date you remember",
            "P13": "Please tell me the product for which you need assistance, for example, Chimney, Cooktop, or Oven,  etc",
            "P12_check":"Is your product of the Faeber brand, Please answer in Yes, or No",
            # New Prompts
            "P6":"We already have an open ticket registered under your mobile number. I have escalated your request and our escalation team will get in touch with you shortly",
            "P2" : "Please tell me if you’d like to register a complaint, request a service, or book an install-ay-shun",

            "P4_update_pincode" :"Please provide your new 6 digit pincode",

            "P10": "Please provide your 6 digit pincode",
            "P12_diff_brand": "Please tell me your brand name, for example, Yurodomo, or Frankie",
            "P12_dtmf": "Sorry, we couldn’t recognize the brand you mentioned. Please press 1, for Yurodomo and 2, for Frankie",
            "P12_start": "Okay, we’re proceeding with the registration of your product",

            "P1_dtmf" : "Sorry, I couldn't understand you. Please press 1, for hindi, and 2, for english",
            "P2_qid_miss" :"Currently I can help you with a service request, a new complaint, or an install-ay-shun. How may I assist you",
            "P2_qid_present" : "I hope that answered your question. Do you still need help with a service request, a new complaint, or an install-ay-shun",
            "P3" : "I can't see any record for this number. For your current request, would you like to use this number itself, or a different one? Please say Yes, for this number, and No, to use a different number",
            "P4_start": "As per our records, your number is registered under the name",
            "P4_pincode" : "with the Pincode",
            "P4_end": "Shall I proceed with these details",
            "P5":"Please provide the different number you'd like to use",
            "P8" : "please provide your full name",
            "P9" : "Please provide your complete address",
            "P11": "Okay, I will register a new request for it",
            "P11_start":"Please let me know, the brand name, and product name, that you want help with? For example, Faeber Chimney",
            "P16":"Thank you! Your request has been registered. A technician will visit you within 24 to 48 hours. Your Ticket ID has been generated, and it will also be sent to you via SMS",
            "P18":"Is there anything else I can assist you with today",
            "P18_1":"Please tell me, how can I help you",
            "P19":"Thank you and have a good day",
            "P_DuplicateTicket":"I have escalated your request, and our Escalation Team will get in touch with you shortly",
            "P_Repeat":"Sorry, we couldn’t hear you properly. Please say it again",
            "P_na":"We are unable to assist you right now, but we have sent you a message on WhatsApp. You can continue your request there. Thank You And have A nice day",
            "DTMF_number":"Sorry I could not understand you, please type the pincode",
            "DTMF_number_again":"The number you entered is not correct, please type the number again",
            "Unable_to_get_number":"Sorry, we couldn't register your number properly, please try again later",
            "validation_number":"Is this correct"
    }

    path = os.getcwd()
    for x in jsonObj.keys():
        fileName = path + "/" + x + ".wav"
        print(fileName)
        ttsObj.getAudio(fileName, jsonObj[x])
        time.sleep(1)

