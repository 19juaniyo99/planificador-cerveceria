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

# Demanda base
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
    horas_objetivo: int = 40
    dias_descanso_input: List[str] = [] 
    tipo_turno_input: str = "Indiferente"
    rol_especifico: str = "-"      

class EventoManualInput(BaseModel):
    nombre: str
    fecha: str       
    hora_inicio: str 
    duracion: int    
    personal_extra: int 

class PlanificadorInput(BaseModel):
    fecha_inicio: str 
    empleados: List[EmpleadoInput]
    eventos_manuales: List[EventoManualInput] = [] 

# --- 3. FUNCIONES AUXILIARES ---
def solapa_evento(bid, hora_evento_str, duracion):
    h_inicio_ev = int(hora_evento_str.split(":")[0])
    h_fin_ev = h_inicio_ev + duracion
    b_inicio = BANDAS[bid]["start"]
    b_fin = BANDAS[bid]["end"]
    return max(b_inicio, h_inicio_ev) < min(b_fin, h_fin_ev)

def get_dias_semana(fecha_inicio_str):
    start = datetime.strptime(fecha_inicio_str, "%Y-%m-%d").date()
    return [start + timedelta(days=i) for i in range(7)]

def parse_dias_descanso(lista_letras, dia_semana_idx):
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
        
        empleados_activos = [e for e in datos.empleados if e.horas_objetivo > 0]
        mapa_emp = {e.nombre: e for e in empleados_activos}
        nombres = list(mapa_emp.keys())
        
        shifts = {} 
        vacs = {}

        # VARIABLES
        for d_idx, d_str in enumerate(dias_str):
            for b in BANDAS:
                bid = b["id"]
                vacs[(d_idx, bid)] = model.NewIntVar(0, 15, f'v_{d_idx}_{bid}')
                for n in nombres:
                    shifts[(n, d_idx, bid)] = model.NewBoolVar(f's_{n}_{d_idx}_{bid}')

        # RESTRICCIONES DE DEMANDA
        for d_idx, fecha in enumerate(fechas):
            d_str = dias_str[d_idx]
            dia_sem = fecha.weekday()
            eventos_hoy = [ev for ev in datos.eventos_manuales if ev.fecha == d_str]
            
            for b in BANDAS:
                bid = b["id"]
                demanda_total = DEMANDA_BASE[dia_sem][bid]
                for ev in eventos_hoy:
                    if solapa_evento(bid, ev.hora_inicio, ev.duracion):
                        demanda_total += ev.personal_extra

                total_trabajando = sum(shifts[(n, d_idx, bid)] for n in nombres)
                model.Add(total_trabajando + vacs[(d_idx, bid)] >= demanda_total)
                
                if b["es_apertura"]:
                    fijos_apertura = [shifts[(n, d_idx, bid)] for n in nombres if mapa_emp[n].rol == "fijo"]
                    model.Add(sum(fijos_apertura) >= 2)

        # RESTRICCIONES DE EMPLEADOS
        for n in nombres:
            emp = mapa_emp[n]
            
            # Descansos
            for d_idx in range(7):
                if parse_dias_descanso(emp.dias_descanso_input, d_idx):
                    for b in BANDAS: model.Add(shifts[(n, d_idx, b["id"])] == 0)

            # Extras
            if emp.rol == "extra":
                for d_idx in range(7):
                    dia_sem = fechas[d_idx].weekday()
                    if dia_sem <= 3: 
                        for b in BANDAS: model.Add(shifts[(n, d_idx, b["id"])] == 0)
                    elif dia_sem == 4: 
                        for b in BANDAS:
                            if b["start"] < 19: model.Add(shifts[(n, d_idx, b["id"])] == 0)

            # Lógica Diaria
            for d_idx in range(7):
                trabaja_banda = [shifts[(n, d_idx, b["id"])] for b in BANDAS]
                trabaja_hoy = model.NewBoolVar(f'tr_{n}_{d_idx}')
                model.Add(sum(trabaja_banda) > 0).OnlyEnforceIf(trabaja_hoy)
                model.Add(sum(trabaja_banda) == 0).OnlyEnforceIf(trabaja_hoy.Not())

                # --- NUEVA REGLA: FLEXIBILIDAD DIARIA (7h - 10h) ---
                horas_hoy = sum(shifts[(n, d_idx, b["id"])] * b["duracion"] for b in BANDAS)
                
                # Si trabaja hoy, debe hacer MÍNIMO 7 horas y MÁXIMO 10 (o lo que permita la ley)
                if emp.rol == "fijo":
                    model.Add(horas_hoy >= 7).OnlyEnforceIf(trabaja_hoy)
                    model.Add(horas_hoy <= 11).OnlyEnforceIf(trabaja_hoy) # Dejamos margen hasta 11 por si acaso

                # --- Roles Específicos ---
                if emp.rol_especifico.lower() == "cierre":
                    model.Add(shifts[(n, d_idx, 5)] == 1).OnlyEnforceIf(trabaja_hoy)
                    emp.tipo_turno_input = "Corrido"
                if emp.rol_especifico.lower() == "apertura":
                    model.Add(shifts[(n, d_idx, 0)] == 1).OnlyEnforceIf(trabaja_hoy)
                
                # --- Turno Corrido vs Partido ---
                es_siempre_corrido = n.lower() in ["aroa", "marina"]
                quiere_corrido = emp.tipo_turno_input.lower() == "corrido"
                
                if es_siempre_corrido or quiere_corrido:
                    transiciones = model.NewIntVar(0, 2, f'trans_{n}_{d_idx}')
                    b_vars = [0] + trabaja_banda + [0]
                    lista_trans = []
                    for k in range(len(b_vars)-1):
                        diff = model.NewIntVar(0, 1, f'd_{n}_{d_idx}_{k}')
                        model.Add(diff != b_vars[k] - b_vars[k+1])
                        lista_trans.append(diff)
                    model.Add(sum(lista_trans) <= 2)
                else: 
                    # Partido (Simplificado para permitir flexibilidad)
                    model.AddImplication(shifts[(n, d_idx, 0)], shifts[(n, d_idx, 1)]) # 12 lleva a 13
                    # Descanso minimo 3h
                    model.AddImplication(shifts[(n, d_idx, 1)], shifts[(n, d_idx, 2)]).OnlyEnforceIf(shifts[(n, d_idx, 3)])
                    model.AddImplication(shifts[(n, d_idx, 2)], shifts[(n, d_idx, 3)]).OnlyEnforceIf(shifts[(n, d_idx, 4)])

            # --- NUEVA REGLA: CONTROL SEMANAL FLEXIBLE ---
            horas_sem = sum(shifts[(n, d, b["id"])] * b["duracion"] for d in range(7) for b in BANDAS)
            
            # Margen de seguridad: Permitimos que falten hasta 5 horas o sobren 8.
            # Esto absorbe los días de 7 horas.
            model.Add(horas_sem >= emp.horas_objetivo - 5) 
            model.Add(horas_sem <= emp.horas_objetivo + 8)

        # OBJETIVO
        total_vacs = sum(vacs.values())
        total_horas = sum(shifts[(n, d, b["id"])] * b["duracion"] for n in nombres for d in range(len(dias_str)) for b in BANDAS)
        model.Minimize(total_vacs * 100000 + total_horas)

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
                    if nv > 0: quien.append(f"FALTAN {nv}")
                    dia_obj["turnos"].append({"hora": b["nombre"], "personal": quien})
                res["dias"].append(dia_obj)
            return res
        return {"status": "IMPOSSIBLE", "msg": "No se encontró solución."}

    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}
