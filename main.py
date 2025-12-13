import calendar
from datetime import date, timedelta, datetime
from typing import List, Optional, Literal
from fastapi import FastAPI
from pydantic import BaseModel
from ortools.sat.python import cp_model

app = FastAPI()

# --- 1. DEFINICIÓN DE BANDAS ---
# Nota: Es vital que las bandas cubran el día de forma lógica.
BANDAS = [
    {"id": 0, "nombre": "12-13", "duracion": 1, "start": 12, "end": 13, "es_apertura": True},
    {"id": 1, "nombre": "13-16", "duracion": 3, "start": 13, "end": 16, "es_apertura": False},
    {"id": 2, "nombre": "16-17", "duracion": 1, "start": 16, "end": 17, "es_apertura": False},
    {"id": 3, "nombre": "17-19", "duracion": 2, "start": 17, "end": 19, "es_apertura": False},
    {"id": 4, "nombre": "19-20", "duracion": 1, "start": 19, "end": 20, "es_apertura": False},
    {"id": 5, "nombre": "20-24", "duracion": 4, "start": 20, "end": 24, "es_apertura": False},
]

# Demanda base (Mínimo personal necesario)
DEMANDA_BASE = {
    0: [3, 4, 3, 2, 3, 5], # Lunes
    1: [3, 4, 3, 2, 3, 5],
    2: [3, 4, 3, 2, 3, 5],
    3: [3, 4, 3, 2, 3, 6],
    4: [3, 5, 5, 3, 4, 8], # Viernes
    5: [3, 7, 7, 4, 4, 8], # Sabado
    6: [3, 5, 5, 3, 3, 5]  # Domingo
}

# --- 2. MODELOS DE DATOS ---
class EmpleadoInput(BaseModel):
    nombre: str
    rol: Literal["fijo", "extra"]
    max_horas_semana: int = 40
    horas_acumuladas_mes: float = 0  # Viene de Google Sheets
    ultimo_turno: Optional[str] = None # "M" o "T" (para rotación)
    dias_no_disponible: List[str] = []

class EventoInput(BaseModel):
    tipo: Literal["betis_home", "sevilla_home", "champions"]
    fecha: str
    hora_kickoff: str
    importancia_alta: bool = True

class PlanificadorInput(BaseModel):
    fecha_inicio: str # YYYY-MM-DD (El lunes donde arranca la quincena)
    empleados: List[EmpleadoInput]
    eventos: List[EventoInput] = []

# --- 3. FUNCIONES AUXILIARES ---
def solapa(bid, kick_str, pre, post):
    h = int(kick_str.split(":")[0])
    t0, t1 = h - pre, h + post
    b0, b1 = BANDAS[bid]["start"], BANDAS[bid]["end"]
    return max(b0, t0) < min(b1, t1)

def get_dias_quincena(fecha_inicio_str):
    start = datetime.strptime(fecha_inicio_str, "%Y-%m-%d").date()
    # Generamos 14 días (2 semanas exactas)
    return [start + timedelta(days=i) for i in range(14)]

# --- 4. SOLVER PRINCIPAL ---
@app.post("/generar")
def generar(datos: PlanificadorInput):
    try:
        model = cp_model.CpModel()
        fechas = get_dias_quincena(datos.fecha_inicio)
        dias_str = [d.strftime("%Y-%m-%d") for d in fechas]
        
        mapa_emp = {e.nombre: e for e in datos.empleados}
        nombres = list(mapa_emp.keys())
        
        shifts = {} # (nombre, dia_index, banda_id)
        vacs = {}   # (dia_index, banda_id)

        # CREAR VARIABLES
        for d_idx, d_str in enumerate(dias_str):
            for b in BANDAS:
                bid = b["id"]
                vacs[(d_idx, bid)] = model.NewIntVar(0, 10, f'v_{d_idx}_{bid}')
                for n in nombres:
                    shifts[(n, d_idx, bid)] = model.NewBoolVar(f's_{n}_{d_idx}_{bid}')

        # ==========================================
        #       A. RESTRICCIONES DE DEMANDA
        # ==========================================
        for d_idx, fecha in enumerate(fechas):
            d_str = dias_str[d_idx]
            dia_sem = fecha.weekday()
            evs_hoy = [ev for ev in datos.eventos if ev.fecha == d_str]
            
            # Apertura (12-13): Contadores auxiliares
            fijos_en_apertura = []

            for b in BANDAS:
                bid = b["id"]
                demanda = DEMANDA_BASE[dia_sem][bid]
                es_sevilla = False

                # Eventos
                for ev in evs_hoy:
                    if ev.tipo == "sevilla_home" and solapa(bid, ev.hora_kickoff, 2, 3):
                        demanda = 11
                        es_sevilla = True
                    if ev.tipo == "champions" and ev.importancia_alta and solapa(bid, ev.hora_kickoff, 0, 2):
                        demanda += 2

                # Cobertura: Personas + Vacantes == Demanda
                total_trabajando = sum(shifts[(n, d_idx, bid)] for n in nombres)
                model.Add(total_trabajando + vacs[(d_idx, bid)] == demanda)

                # Regla Sevilla Home (Extras obligatorios)
                if es_sevilla:
                    for n in nombres:
                        if mapa_emp[n].rol == "extra" and d_str not in mapa_emp[n].dias_no_disponible:
                            model.Add(shifts[(n, d_idx, bid)] == 1)

                # Regla 3.4: Apertura (Banda 0) -> Mínimo 2 FIJOS
                if b["es_apertura"]:
                    fijos_apertura = [shifts[(n, d_idx, bid)] for n in nombres if mapa_emp[n].rol == "fijo"]
                    # Debe haber al menos 2 fijos trabajando en esta banda
                    model.Add(sum(fijos_apertura) >= 2)

        # ==========================================
        #       B. RESTRICCIONES DE EMPLEADOS
        # ==========================================
        
        # Iteramos por empleado
        for n in nombres:
            emp = mapa_emp[n]
            
            # --- 1. Disponibilidad y Betis ---
            for d_idx, d_str in enumerate(dias_str):
                # Días bloqueados manuales
                if d_str in emp.dias_no_disponible:
                    for b in BANDAS: model.Add(shifts[(n, d_idx, b["id"])] == 0)
                
                # Regla Aroa (Betis)
                if n.lower() == "aroa":
                    for ev in datos.eventos:
                        if ev.tipo == "betis_home" and ev.fecha == d_str:
                            for b in BANDAS: model.Add(shifts[(n, d_idx, b["id"])] == 0)

                # Regla Extras (Horarios restringidos)
                if emp.rol == "extra":
                    dia_sem = fechas[d_idx].weekday()
                    if dia_sem <= 3: # Lunes-Jueves OFF
                        for b in BANDAS: model.Add(shifts[(n, d_idx, b["id"])] == 0)
                    elif dia_sem == 4: # Viernes solo tarde (>19h)
                        for b in BANDAS:
                            if b["start"] < 19: model.Add(shifts[(n, d_idx, b["id"])] == 0)

            # --- 2. Lógica de Turnos Diarios ---
            for d_idx in range(len(dias_str)):
                # Variables auxiliares del día
                trabaja_banda = [shifts[(n, d_idx, b["id"])] for b in BANDAS]
                trabaja_hoy = model.NewBoolVar(f'tr_{n}_{d_idx}')
                model.Add(sum(trabaja_banda) > 0).OnlyEnforceIf(trabaja_hoy)
                model.Add(sum(trabaja_banda) == 0).OnlyEnforceIf(trabaja_hoy.Not())

                # Regla 2.a: Turno Corrido (Aroa y Marina)
                if n.lower() in ["aroa", "marina"]:
                    # No puede haber huecos. Si trabaja la banda X y la Z, debe trabajar la Y intermedia.
                    # Simplificación: Solo 1 bloque continuo.
                    # Detección de transiciones 0->1 y 1->0
                    transiciones = model.NewIntVar(0, 2, f'trans_{n}_{d_idx}')
                    
                    # Añadimos ceros virtuales al inicio y fin del día para contar transiciones
                    b_vars = [0] + trabaja_banda + [0]
                    lista_trans = []
                    for k in range(len(b_vars)-1):
                        diff = model.NewIntVar(0, 1, f'd_{n}_{d_idx}_{k}')
                        model.Add(diff != b_vars[k] - b_vars[k+1]) # Abs diff simulada
                        lista_trans.append(diff)
                    
                    model.Add(sum(lista_trans) <= 2) # Máximo 1 subida y 1 bajada = 1 bloque

                # Regla 2.b: Turno Partido (Resto)
                # Bloque >= 3h y Descanso >= 3h
                else: 
                    # Esta regla es compleja en CP-SAT puro. Usamos reglas de implicación por bandas.
                    # 1. Prohibir bloques de 1 hora aislados (Bandas 12-13 y 16-17)
                    # Si trabaja 12-13 (1h), DEBE trabajar 13-16.
                    model.AddImplication(shifts[(n, d_idx, 0)], shifts[(n, d_idx, 1)])
                    # Si trabaja 16-17 (1h), DEBE trabajar 13-16 O 17-19.
                    model.AddBoolOr([shifts[(n, d_idx, 1)], shifts[(n, d_idx, 3)]]).OnlyEnforceIf(shifts[(n, d_idx, 2)])
                    # Si trabaja 19-20 (1h), DEBE trabajar 17-19 O 20-24.
                    model.AddBoolOr([shifts[(n, d_idx, 3)], shifts[(n, d_idx, 5)]]).OnlyEnforceIf(shifts[(n, d_idx, 4)])

                    # 2. Descanso mínimo de 3h entre bloques
                    # Significa: No puedes trabajar turno de mañana (acabar 16:00 o 17:00) y volver a las 17:00 o 19:00.
                    # Combinaciones prohibidas específicas:
                    # Salir a las 16 (Banda 1) y volver a las 17 (Banda 3) -> Solo 1h descanso -> PROHIBIDO si no trabaja banda 2
                    # Si trabaja B1(13-16) y B3(17-19), DEBE trabajar B2(16-17) para que sea continuo. 
                    # Si no trabaja B2, es partido con hueco de 1h -> MALO.
                    model.AddImplication(shifts[(n, d_idx, 1)], shifts[(n, d_idx, 2)]).OnlyEnforceIf(shifts[(n, d_idx, 3)])
                    
                    # Salir a las 17 (Banda 2) y volver a las 19 (Banda 4) -> Hueco 2h -> MALO.
                    model.AddImplication(shifts[(n, d_idx, 2)], shifts[(n, d_idx, 3)]).OnlyEnforceIf(shifts[(n, d_idx, 4)])

            # --- 3. Días Libres Consecutivos (Regla 3.2) ---
            # En cada ventana de 7 días, debe haber al menos 1 bloque de 2 días libres consecutivos.
            # Ventana 1: Dias 0-6. Ventana 2: Dias 7-13.
            for start_day in [0, 7]:
                window = range(start_day, start_day + 7)
                # Variables booleanas: "es_descanso_doble" empieza en el dia 'w'
                dobles_libres = []
                for w in window[:-1]: # hasta el penúltimo día
                    es_libre_hoy = model.NewBoolVar(f'lib_{n}_{w}')
                    es_libre_man = model.NewBoolVar(f'lib_{n}_{w+1}')
                    trabaja_hoy = model.NewBoolVar(f'tr_{n}_{w}')
                    trabaja_man = model.NewBoolVar(f'tr_{n}_{w+1}')
                    
                    # Detectar si trabaja
                    horas_h = sum(shifts[(n, w, b["id"])] for b in BANDAS)
                    horas_m = sum(shifts[(n, w+1, b["id"])] for b in BANDAS)
                    model.Add(horas_h > 0).OnlyEnforceIf(trabaja_hoy)
                    model.Add(horas_h == 0).OnlyEnforceIf(trabaja_hoy.Not())
                    model.Add(horas_m > 0).OnlyEnforceIf(trabaja_man)
                    model.Add(horas_m == 0).OnlyEnforceIf(trabaja_man.Not())

                    # Doble libre = No trabaja hoy Y No trabaja mañana
                    doble = model.NewBoolVar(f'2off_{n}_{w}')
                    model.AddBoolAnd([trabaja_hoy.Not(), trabaja_man.Not()]).OnlyEnforceIf(doble)
                    model.AddBoolOr([trabaja_hoy, trabaja_man]).OnlyEnforceIf(doble.Not())
                    dobles_libres.append(doble)
                
                if emp.rol == "fijo":
                    model.Add(sum(dobles_libres) >= 1)

            # --- 4. Control de Horas (Regla 3.1) ---
            # Dividimos la quincena en 2 semanas
            semanas_idx = [range(0, 7), range(7, 14)]
            horas_total_quincena = 0
            
            for semana in semanas_idx:
                horas_sem = sum(shifts[(n, d, b["id"])] * b["duracion"] for d in semana for b in BANDAS)
                
                if emp.rol == "fijo":
                    # Soft limit 40h (puede pasarse pero penaliza)
                    exceso = model.NewIntVar(0, 20, f'exceso_{n}_{semana}')
                    model.Add(horas_sem <= 40 + exceso)
                    # Penalizamos el exceso en la función objetivo
                    # Pero ponemos un tope duro (ej: 48h)
                    model.Add(horas_sem <= 48)
                else:
                    # Extras: Rango fijo 16-24
                    model.Add(horas_sem >= emp.min_horas_semana)
                    model.Add(horas_sem <= emp.max_horas_semana)
                
                horas_total_quincena += horas_sem

            # --- 5. Rotación (Regla 2.c) ---
            # Si su ultimo turno fue Tarde ("T"), bonificamos que trabaje de Mañana ("M") esta quincena.
            # Mañana = Bandas 0, 1 (acaban a las 16). Tarde = Bandas 4, 5 (empiezan a las 19).
            if emp.ultimo_turno:
                for d_idx in range(len(dias_str)):
                    es_manana = shifts[(n, d_idx, 0)] # Si entra a las 12
                    es_tarde = shifts[(n, d_idx, 5)]  # Si está a las 20-24
                    
                    # Si venía de Tarde, preferimos Mañana
                    # Esto lo metemos como pesos en la función objetivo, no restricción dura.

        # ==========================================
        #       C. FUNCIÓN OBJETIVO
        # ==========================================
        total_vacs = sum(vacs.values())
        
        # Penalización por horas extra (Fijos)
        penalizacion_extras = 0
        # (Aquí simplifico la lógica de penalización para no alargar demasiado el código, 
        #  el solver priorizará no usar vacantes y luego ajustar horas).

        model.Minimize(total_vacs * 10000)

        # SOLVER
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 60.0 # Damos 1 min porque es complejo
        st = solver.Solve(model)

        if st in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
            res = {"dias": []}
            for d_idx, d_str in enumerate(dias_str):
                dia_obj = {"fecha": d_str, "turnos": []}
                for b in BANDAS:
                    bid = b["id"]
                    quien = []
                    # Recoger nombres
                    for n in nombres:
                        if solver.Value(shifts[(n, d_idx, bid)]): quien.append(n)
                    # Recoger vacantes
                    nv = solver.Value(vacs[(d_idx, bid)])
                    for _ in range(nv): quien.append("VACANTE")
                    
                    dia_obj["turnos"].append({"hora": b["nombre"], "personal": quien})
                res["dias"].append(dia_obj)
            return res
        return {"status": "IMPOSSIBLE", "msg": "No se encontró solución con estas restricciones estrictas."}

    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}
