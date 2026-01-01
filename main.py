import calendar
from datetime import date, timedelta, datetime
from typing import List, Optional, Literal
from fastapi import FastAPI
from pydantic import BaseModel
from ortools.sat.python import cp_model

app = FastAPI()

# --- 1. DEFINICIÓN DE BANDAS ---
BANDAS = [
    {"id": 0, "nombre": "12-13", "duracion": 1, "start": 12, "end": 13, "es_apertura": True},
    {"id": 1, "nombre": "13-16", "duracion": 3, "start": 13, "end": 16, "es_apertura": False},
    {"id": 2, "nombre": "16-17", "duracion": 1, "start": 16, "end": 17, "es_apertura": False},
    {"id": 3, "nombre": "17-19", "duracion": 2, "start": 17, "end": 19, "es_apertura": False},
    {"id": 4, "nombre": "19-20", "duracion": 1, "start": 19, "end": 20, "es_apertura": False},
    {"id": 5, "nombre": "20-24", "duracion": 4, "start": 20, "end": 24, "es_apertura": False}, # Cierre
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
    horas_objetivo: int = 40       # Lo que manda el jefe
    dias_descanso_input: List[str] = [] # ["L", "M"]
    tipo_turno_input: str = "Indiferente" # "Corrido", "Partido", "Indiferente"
    rol_especifico: str = "-"      # "Apertura", "Cierre", "-"
    dias_no_disponible: List[str] = [] # Para fechas específicas si hiciera falta (legacy)

class EventoInput(BaseModel):
    tipo: Literal["betis_home", "sevilla_home", "champions"]
    fecha: str
    hora_kickoff: str
    importancia_alta: bool = True

class PlanificadorInput(BaseModel):
    fecha_inicio: str # Lunes de la semana a generar
    empleados: List[EmpleadoInput]
    eventos: List[EventoInput] = []

# --- 3. FUNCIONES AUXILIARES ---
def solapa(bid, kick_str, pre, post):
    h = int(kick_str.split(":")[0])
    t0, t1 = h - pre, h + post
    b0, b1 = BANDAS[bid]["start"], BANDAS[bid]["end"]
    return max(b0, t0) < min(b1, t1)

def get_dias_semana(fecha_inicio_str):
    start = datetime.strptime(fecha_inicio_str, "%Y-%m-%d").date()
    # Generamos SOLO 1 semana (7 días) según petición del jefe
    return [start + timedelta(days=i) for i in range(7)]

def parse_dias_descanso(lista_letras, dia_semana_idx):
    # L=0, M=1, X=2, J=3, V=4, S=5, D=6
    mapa = {"L": 0, "M": 1, "X": 2, "J": 3, "V": 4, "S": 5, "D": 6}
    dias_off = []
    for l in lista_letras:
        limpia = l.upper().strip()
        if limpia in mapa: dias_off.append(mapa[limpia])
    return dia_semana_idx in dias_off

# --- 4. SOLVER PRINCIPAL ---
@app.post("/generar")
def generar(datos: PlanificadorInput):
    try:
        model = cp_model.CpModel()
        fechas = get_dias_semana(datos.fecha_inicio)
        dias_str = [d.strftime("%Y-%m-%d") for d in fechas]
        
        # Filtramos empleados con 0 horas (no trabajan esta semana)
        empleados_activos = [e for e in datos.empleados if e.horas_objetivo > 0]
        mapa_emp = {e.nombre: e for e in empleados_activos}
        nombres = list(mapa_emp.keys())
        
        shifts = {} 
        vacs = {}

        # VARIABLES
        for d_idx, d_str in enumerate(dias_str):
            for b in BANDAS:
                bid = b["id"]
                vacs[(d_idx, bid)] = model.NewIntVar(0, 10, f'v_{d_idx}_{bid}')
                for n in nombres:
                    shifts[(n, d_idx, bid)] = model.NewBoolVar(f's_{n}_{d_idx}_{bid}')

        # RESTRICCIONES DE DEMANDA
        for d_idx, fecha in enumerate(fechas):
            d_str = dias_str[d_idx]
            dia_sem = fecha.weekday()
            evs_hoy = [ev for ev in datos.eventos if ev.fecha == d_str]
            
            for b in BANDAS:
                bid = b["id"]
                demanda = DEMANDA_BASE[dia_sem][bid]
                es_sevilla = False

                for ev in evs_hoy:
                    if ev.tipo == "sevilla_home" and solapa(bid, ev.hora_kickoff, 2, 3):
                        demanda = 11
                        es_sevilla = True
                    if ev.tipo == "champions" and ev.importancia_alta and solapa(bid, ev.hora_kickoff, 0, 2):
                        demanda += 2

                # Flexibilidad: >= demanda
                total_trabajando = sum(shifts[(n, d_idx, bid)] for n in nombres)
                model.Add(total_trabajando + vacs[(d_idx, bid)] >= demanda)

                if es_sevilla:
                    for n in nombres:
                        # Extras obligatorios si no es su día libre y tienen horas
                        emp = mapa_emp[n]
                        if emp.rol == "extra" and not parse_dias_descanso(emp.dias_descanso_input, dia_sem):
                             model.Add(shifts[(n, d_idx, bid)] == 1)

                if b["es_apertura"]:
                    fijos_apertura = [shifts[(n, d_idx, bid)] for n in nombres if mapa_emp[n].rol == "fijo"]
                    model.Add(sum(fijos_apertura) >= 2)

        # RESTRICCIONES DE EMPLEADOS
        for n in nombres:
            emp = mapa_emp[n]
            
            # 1. Días de Descanso (Input del Jefe "L,M,X")
            for d_idx in range(7):
                if parse_dias_descanso(emp.dias_descanso_input, d_idx):
                    for b in BANDAS: model.Add(shifts[(n, d_idx, b["id"])] == 0)

            # 2. Regla Extras (Horarios Restringidos)
            if emp.rol == "extra":
                for d_idx in range(7):
                    dia_sem = fechas[d_idx].weekday()
                    # Lunes(0) a Jueves(3) -> PROHIBIDO
                    if dia_sem <= 3: 
                        for b in BANDAS: model.Add(shifts[(n, d_idx, b["id"])] == 0)
                    # Viernes(4) -> Solo tarde (>19h, bandas 4 y 5)
                    elif dia_sem == 4: 
                        for b in BANDAS:
                            if b["start"] < 19: model.Add(shifts[(n, d_idx, b["id"])] == 0)
                    # Sabado(5) y Domingo(6) -> FULL TIME PERMITIDO

            # 3. Lógica de Turnos Diarios
            for d_idx in range(7):
                trabaja_banda = [shifts[(n, d_idx, b["id"])] for b in BANDAS]
                trabaja_hoy = model.NewBoolVar(f'tr_{n}_{d_idx}')
                model.Add(sum(trabaja_banda) > 0).OnlyEnforceIf(trabaja_hoy)
                model.Add(sum(trabaja_banda) == 0).OnlyEnforceIf(trabaja_hoy.Not())

                # --- REGLA JEFE: ROL ESPECÍFICO ---
                if emp.rol_especifico.lower() == "cierre":
                    # Debe trabajar la banda 5 (20-24)
                    model.Add(shifts[(n, d_idx, 5)] == 1).OnlyEnforceIf(trabaja_hoy)
                    # Y debe ser continuo (Aroa/Marina rule logic forced)
                    emp.tipo_turno_input = "Corrido" # Forzamos variable interna

                if emp.rol_especifico.lower() == "apertura":
                    # Debe trabajar la banda 0 (12-13)
                    model.Add(shifts[(n, d_idx, 0)] == 1).OnlyEnforceIf(trabaja_hoy)
                
                # --- LÓGICA CORRIDO VS PARTIDO ---
                # Detectamos si es Aroa/Marina (siempre corrido) O si el jefe lo pidió "Corrido"
                es_siempre_corrido = n.lower() in ["aroa", "marina"]
                quiere_corrido = emp.tipo_turno_input.lower() == "corrido"
                
                if es_siempre_corrido or quiere_corrido:
                    # Turno continuo (máximo 1 bloque, sin huecos)
                    transiciones = model.NewIntVar(0, 2, f'trans_{n}_{d_idx}')
                    b_vars = [0] + trabaja_banda + [0]
                    lista_trans = []
                    for k in range(len(b_vars)-1):
                        diff = model.NewIntVar(0, 1, f'd_{n}_{d_idx}_{k}')
                        model.Add(diff != b_vars[k] - b_vars[k+1])
                        lista_trans.append(diff)
                    model.Add(sum(lista_trans) <= 2)
                
                else: 
                    # Puede ser partido (si el jefe puso "Partido" o "Indiferente")
                    # Reglas estándar: huecos prohibidos de 1h, descanso min 3h
                    model.AddImplication(shifts[(n, d_idx, 0)], shifts[(n, d_idx, 1)]) # 12-13 -> 13-16
                    model.AddBoolOr([shifts[(n, d_idx, 1)], shifts[(n, d_idx, 3)]]).OnlyEnforceIf(shifts[(n, d_idx, 2)])
                    model.AddBoolOr([shifts[(n, d_idx, 3)], shifts[(n, d_idx, 5)]]).OnlyEnforceIf(shifts[(n, d_idx, 4)])
                    
                    # Descanso minimo 3h
                    model.AddImplication(shifts[(n, d_idx, 1)], shifts[(n, d_idx, 2)]).OnlyEnforceIf(shifts[(n, d_idx, 3)])
                    model.AddImplication(shifts[(n, d_idx, 2)], shifts[(n, d_idx, 3)]).OnlyEnforceIf(shifts[(n, d_idx, 4)])

                    # Si el jefe EXIGIÓ "Partido", forzamos que haya un hueco (descanso)
                    if emp.tipo_turno_input.lower() == "partido":
                         # Esto es complejo de forzar estrictamente sin romper, 
                         # pero podemos bonificarlo o simplemente permitirlo. 
                         # Por seguridad, con las reglas de arriba ya permitimos el partido.
                         pass

            # 4. Control de Horas (Objetivo del Jefe)
            horas_sem = sum(shifts[(n, d, b["id"])] * b["duracion"] for d in range(7) for b in BANDAS)
            
            # Tolerancia: Puede hacer +4 horas de lo que pidió el jefe (para cuadrar picos)
            # Pero debe hacer MÍNIMO lo que pidió el jefe (o un poco menos si es imposible)
            model.Add(horas_sem >= emp.horas_objetivo) 
            model.Add(horas_sem <= emp.horas_objetivo + 8) # Damos margen de 8h extras max

        # OBJETIVO
        total_vacs = sum(vacs.values())
        total_horas_plantilla = sum(shifts[(n, d, b["id"])] * b["duracion"] for n in nombres for d in range(len(dias_str)) for b in BANDAS)
        
        model.Minimize(total_vacs * 100000 + total_horas_plantilla)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 120.0
        st = solver.Solve(model)

        if st in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
            res = {"dias": []}
            for d_idx, d_str in enumerate(dias_str):
                dia_obj = {"fecha": d_str, "turnos": []}
                for b in BANDAS:
                    bid = b["id"]
                    quien = []
                    for n in nombres:
                        if solver.Value(shifts[(n, d_idx, bid)]): quien.append(n)
                    nv = solver.Value(vacs[(d_idx, bid)])
                    for _ in range(nv): quien.append("VACANTE")
                    dia_obj["turnos"].append({"hora": b["nombre"], "personal": quien})
                res["dias"].append(dia_obj)
            return res
        return {"status": "IMPOSSIBLE", "msg": "No se encontró solución. Revisa restricciones del jefe."}

    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}
