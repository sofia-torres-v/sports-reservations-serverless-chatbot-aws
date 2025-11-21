"""
Handler para ReserveCourtIntent
"""

import os
import boto3
import uuid
from utils import (
    get_slot_value, 
    close_intent, 
    get_current_timestamp_ba,
    validate_reservation_time,
    format_date,
    get_current_time_ba,
    elicit_slot,
    delegate
)

dynamodb = boto3.resource('dynamodb')
customers_table = dynamodb.Table(os.environ['CUSTOMERS_TABLE'])
reservations_table = dynamodb.Table(os.environ['RESERVATIONS_TABLE'])

# Costos de canchas (en créditos)
COURT_COSTS = {
    'futbol': 50,
    'fútbol': 50,
    'voley': 30,
    'vóley': 30,
    'voleibol': 30
}


def extract_court_type(text):
    """
    Extrae el tipo de cancha del mensaje del usuario
    """
    if not text:
        return None
    
    text_lower = text.lower()
    
    # Buscar fútbol
    if 'futbol' in text_lower or 'fútbol' in text_lower:
        print(f"✅ Detectado: futbol en '{text}'")
        return 'futbol'
    
    # Buscar voley
    if 'voley' in text_lower or 'vóley' in text_lower or 'voleibol' in text_lower:
        print(f"✅ Detectado: voley en '{text}'")
        return 'voley'
    
    print(f"❌ No se detectó tipo de cancha en: '{text}'")
    return None


def set_slot(slots, slot_name, value):
    """
    Establece un slot programáticamente
    """
    slots[slot_name] = {
        'shape': 'Scalar',
        'value': {
            'originalValue': value,
            'interpretedValue': value,
            'resolvedValues': [value]
        }
    }


def handle_reserve_court(event):
    """
    Maneja el intent de reserva de cancha
    """
    invocation_source = event['invocationSource']
    slots = event['sessionState']['intent']['slots']
    session_attributes = event.get('sessionState', {}).get('sessionAttributes', {})
    
    # Extraer valores de los slots
    customer_dni = get_slot_value(slots, 'sl_customer_dni')
    court_type = get_slot_value(slots, 'slt_court_types')
    date = get_slot_value(slots, 'sl_date')
    time = get_slot_value(slots, 'sl_time')
    confirmation = get_slot_value(slots, 'sl_confirmation', '')
    
    print(f"🔍 invocationSource: {invocation_source}")
    print(f"📋 Session Attributes: {session_attributes}")
    print(f"📋 Slots - DNI: {customer_dni}, Cancha: {court_type}, Fecha: {date}, Hora: {time}")
    
    # ==========================================
    # PASO 0: Pre-llenar tipo de cancha si no existe
    # ==========================================
    if not court_type:
        # Intentar extraer del mensaje original (viene de Connect)
        user_message = session_attributes.get('UserOriginalMessage', '')
        
        # También del transcript actual
        input_transcript = event.get('inputTranscript', '')
        
        # Buscar en ambos
        detected = extract_court_type(user_message) or extract_court_type(input_transcript)
        
        if detected:
            print(f"✅ Pre-llenando slot con: {detected}")
            set_slot(slots, 'slt_court_types', detected)
            court_type = detected
    
    # ==========================================
    # PARTE 1: VALIDACIONES (DialogCodeHook)
    # ==========================================
    if invocation_source == 'DialogCodeHook':
        print("✅ Validando slots...")
        
        # Validación 1: Usuario canceló
        if confirmation and confirmation.lower().strip() in ['no', 'nop', 'negativo', 'cancelar', 'cancelo', 'nunca', 'no quiero']:
            print("❌ Usuario canceló")
            return close_intent(
                event,
                'Fulfilled',
                'Entendido, reserva cancelada. ¿En qué más puedo ayudarte?'
            )
        
        # Validación 2: Fecha/hora en el pasado
        if date and time:
            if not validate_reservation_time(date, time):
                print("❌ Fecha/hora en el pasado")
                now_ba = get_current_time_ba()
                slots['sl_date'] = None
                slots['sl_time'] = None
                return elicit_slot(
                    event,
                    'sl_date',
                    f'❌ Ese horario ({format_date(date)} a las {time}) ya pasó.\n'
                    f'Hora actual: {now_ba.strftime("%d/%m/%Y %H:%M")}\n\n'
                    f'Por favor elige una fecha futura. Ejemplo: 30/11/2025'
                )
        
        # Todo OK, continuar
        return delegate(event)
    
    # ==========================================
    # PARTE 2: FULFILLMENT (crear reserva)
    # ==========================================
    if invocation_source == 'FulfillmentCodeHook':
        print("✅ Creando reserva...")
        
        if court_type:
            court_type = court_type.lower()
        
        try:
            # 1. Verificar cliente existe
            response = customers_table.get_item(Key={'customer_dni': customer_dni})
            if 'Item' not in response:
                return close_intent(
                    event,
                    'Fulfilled',
                    f'❌ No encontramos cuenta con DNI {customer_dni}.\n'
                    f'Primero carga créditos: "quiero cargar créditos"'
                )
            
            customer = response['Item']
            current_credits = int(customer.get('credits', 0))
            
            # 2. Calcular costo
            cost = COURT_COSTS.get(court_type, 50)
            
            # 3. Verificar créditos
            if current_credits < cost:
                return close_intent(
                    event,
                    'Fulfilled',
                    f'❌ Créditos insuficientes.\n'
                    f'Necesitas: {cost} créditos\n'
                    f'Tienes: {current_credits} créditos\n'
                    f'Faltan: {cost - current_credits} créditos\n\n'
                    f'Carga más: "quiero cargar créditos"'
                )
            
            # 4. Crear reserva
            reservation_id = f"RES-{uuid.uuid4().hex[:8].upper()}"
            
            reservations_table.put_item(
                Item={
                    'reservation_id': reservation_id,
                    'customer_dni': customer_dni,
                    'court_type': court_type,
                    'reservation_date': date,
                    'reservation_time': time,
                    'reservation_datetime': f"{date} {time}",
                    'cost': cost,
                    'status': 'confirmed',
                    'created_at': get_current_timestamp_ba()
                }
            )
            
            # 5. Descontar créditos
            new_credits = current_credits - cost
            customers_table.update_item(
                Key={'customer_dni': customer_dni},
                UpdateExpression='SET credits = :credits',
                ExpressionAttributeValues={':credits': new_credits}
            )
            
            # 6. Confirmar
            return close_intent(
                event,
                'Fulfilled',
                f'✅ ¡Reserva confirmada!\n\n'
                f'📋 Código: {reservation_id}\n'
                f'🏟️ Cancha: {court_type.capitalize()}\n'
                f'📅 Fecha: {format_date(date)}\n'
                f'🕐 Hora: {time}\n'
                f'💰 Costo: {cost} créditos\n\n'
                f'Nuevo saldo: {new_credits} créditos\n\n'
                f'Llega 10 minutos antes. ¡Disfruta!'
            )
        
        except Exception as e:
            print(f"❌ Error: {str(e)}")
            return close_intent(
                event,
                'Failed',
                'Error procesando reserva. Intenta de nuevo.'
            )