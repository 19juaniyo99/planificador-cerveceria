import calendar
from datetime import date, timedelta
from typing import List, Optional, Literal
from fastapi import FastAPI
from pydantic import BaseModel
from ortools.sat.python import cp_model

app = FastAPI()

# --- 1. DATOS FIJOS ---
BANDAS = [
    {"id": 0, "nombre": "12-13", "duracion": 1, "start": 12, "end": 13},
    {"id": 1, "nombre": "13-16", "duracion": 3, "start": 13, "end": 16},
    {"id": 2, "nombre": "16-17", "duracion": 1, "start": 16, "end": 17},
    {"id": 3, "nombre": "17-19", "duracion": 2, "start": 17, "end": 19},
    {"id": 4, "nombre": "19-20", "duracion": 1, "start": 19, "end": 20},
    {"id": 5, "nombre": "20-24", "duracion": 4, "start": 20, "end": 24},
]

DEMANDA_BASE = {
    0: [3, 4, 3, 2, 3, 5], # Lunes
    1: [3, 4, 3, 2, 3, 5],
    2: [3, 4, 3, 2, 3, 5],
    3: [3, 4, 3, 2, 3, 6],
    4: [3, 5, 5, 3, 4, 8],
    5: [3, 7, 7, 4, 4, 8],
    6: [3, 5, 5, 3, 3, 5]
}

# --- 2. MODELOS ---
class Empleado(BaseModel):
    nombre: str
    rol: Literal["fijo", "extra"]
    max_horas_semana: int = 40
    min_horas_semana: int = 16
    dias_no_disponible: List[str] = []

class Evento(BaseModel):
    tipo: Literal["betis_home", "sevilla_home", "champions"]
    fecha: str
    hora_kickoff: str
    importancia_alta: bool = True

class InputPlanificador(BaseModel):
    anio: int
    mes: int
    empleados: List[Empleado]
    eventos: List[Evento] = []

# --- 3. AUXILIARES ---
def get_rango_fechas(anio, mes):
    primer = date(anio, mes, 1)
    inicio = primer - timedelta(days=primer.weekday())
    ultimo = date(anio, mes, calendar.monthrange(anio, mes)[1])
    fin = ultimo + timedelta(days=(6 - ultimo.weekday()))
    dias = []
    curr = inicio
    while curr <= fin:
        dias.append(curr)
        curr += timedelta(days=1)
    return dias

def solapa(bid, kick_str, pre, post):
    h = int(kick_str.split(":")[0])
    t0, t1 = h - pre, h + post
    b0, b1 = BANDAS[bid]["start"], BANDAS[bid]["end"]
    return max(b0, t0) < min(b1, t1)

# --- 4. SOLVER ---
@app.post("/generar")
def generar(datos: InputPlanificador):
    try:
        model = cp_model.CpModel()
        fechas = get_rango_fechas(datos.anio, datos.mes)
        dias_str = [d.strftime("%Y-%m-%d") for d in fechas]
        
        mapa_emp = {e.nombre: e for e in datos.empleados}
        nombres = list(mapa_emp.keys())
        
        shifts = {}
        vacs = {}

        # VARIABLES
        for d in dias_str:
            for b in BANDAS:
                bid = b["id"]
                # Vacantes (permitimos hasta 10 por si hay caos, pero se penalizan)
                vacs[(d, bid)] = model.NewIntVar(0, 10, f'v_{d}_{bid}')
                for n in nombres:
                    shifts[(n, d, bid)] = model.NewBoolVar(f's_{n}_{d}_{bid}')

        # --- A. DEMANDA EXACTA ---
        for i, fecha in enumerate(fechas):
            d_str = dias_str[i]
            dia_sem = fecha.weekday()
            evs_hoy = [ev for ev in datos.eventos if ev.fecha == d_str]
            
            for b in BANDAS:
                bid = b["id"]
                demanda = DEMANDA_BASE[dia_sem][bid]
                es_sevilla = False
                
                # Ajuste por Eventos
                for ev in evs_hoy:
                    if ev.tipo == "sevilla_home" and solapa(bid, ev.hora_kickoff, 2, 3):
                        demanda = 11 # Aquí forzamos 11 exactos
                        es_sevilla = True
                    if ev.tipo == "champions" and ev.importancia_alta and solapa(bid, ev.hora_kickoff, 0, 2):
                        demanda += 2 # Aquí sumamos +2 a la base exacta
                
                # REGLA DE ORO: Suma == Demanda (NO >=)
                # Esto obliga a que sea el número exacto.
                model.Add(sum(shifts[(n, d_str, bid)] for n in nombres) + vacs[(d_str, bid)] == demanda)
                
                # Regla Sevilla: Extras obligatorios presentes
                if es_sevilla:
                    for n in nombres:
                        if mapa_emp[n].rol == "extra" and d_str not in mapa_emp[n].dias_no_disponible:
                            model.Add(shifts[(n, d_str, bid)] == 1)

        # --- B. REGLAS EMPLEADO ---
        chunks = [dias_str[i:i+7] for i in range(0, len(dias_str), 7)]

        for n in nombres:
            emp = mapa_emp[n]
            
            # Disponibilidad
            for bloq in emp.dias_no_disponible:
                if bloq in dias_str:
                    for b in BANDAS: model.Add(shifts[(n, bloq, b["id"])] == 0)
            
            # Betis (Aroa)
            if n.lower() == "aroa":
                for ev in datos.eventos:
                    if ev.tipo == "betis_home" and ev.fecha in dias_str:
                        for b in BANDAS: model.Add(shifts[(n, ev.fecha, b["id"])] == 0)

            # Continuidad (No huecos)
            for d in dias_str:
                model.AddImplication(shifts[(n, d, 0)], shifts[(n, d, 1)])
                model.AddBoolOr([shifts[(n, d, 1)], shifts[(n, d, 3)]]).OnlyEnforceIf(shifts[(n, d, 2)])
                model.AddBoolOr([shifts[(n, d, 2)], shifts[(n, d, 4)]]).OnlyEnforceIf(shifts[(n, d, 3)])
                model.AddBoolOr([shifts[(n, d, 3)], shifts[(n, d, 5)]]).OnlyEnforceIf(shifts[(n, d, 4)])

            # Minimo 4h diarias (Si trabaja)
            for d in dias_str:
                horas_dia = sum(shifts[(n, d, b["id"])] * b["duracion"] for b in BANDAS)
                trabaja_hoy = model.NewBoolVar(f'tr_{n}_{d}')
                model.Add(horas_dia > 0).OnlyEnforceIf(trabaja_hoy)
                model.Add(horas_dia == 0).OnlyEnforceIf(trabaja_hoy.Not())
                model.Add(horas_dia >= 4).OnlyEnforceIf(trabaja_hoy)

            # Semanales
            for chunk in chunks:
                if len(chunk) == 7:
                    horas_sem = sum(shifts[(n, d, b["id"])] * b["duracion"] for d in chunk for b in BANDAS)
                    model.Add(horas_sem <= emp.max_horas_semana)
                    if emp.rol == "extra":
                        model.Add(horas_sem >= emp.min_horas_semana)
                    else:
                        model.Add(horas_sem >= 40)

        # --- C. MINIMO 2 FIJOS ---
        fijos = [n for n in nombres if mapa_emp[n].rol == "fijo"]
        for d in dias_str:
            for b in BANDAS:
                # OJO: Si la demanda exacta es menor que 2 (ej: 1 persona), esto daría conflicto.
                # Asumo que tus demandas siempre son >= 2.
                model.Add(sum(shifts[(n, d, b["id"])] for n in fijos) >= 2)

        # --- D. OPTIMIZACIÓN ---
        # Prioridad suprema: Evitar Vacantes (que significaría que no ha logrado cuadrar el número exacto)
        model.Minimize(sum(vacs.values()))

        # SOLVER
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 45.0
        st = solver.Solve(model)

        if st in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
            res = {"semanas": []}
            for chunk in chunks:
                sem_data = []
                for d in chunk:
                    dia_obj = {"fecha": d, "turnos": []}
                    for b in BANDAS:
                        bid = b["id"]
                        quien = []
                        # Fijos
                        for n in fijos:
                            if solver.Value(shifts[(n, d, bid)]): quien.append(n)
                        # Extras
                        for n in nombres:
                            if mapa_emp[n].rol == "extra" and solver.Value(shifts[(n, d, bid)]): quien.append(n)
                        # Vacantes
                        nv = solver.Value(vacs[(d, bid)])
                        for _ in range(nv): quien.append("VACANTE")
                        
                        dia_obj["turnos"].append({"hora": b["nombre"], "personal": quien})
                    sem_data.append(dia_obj)
                res["semanas"].append(sem_data)
            return res
        return {"status": "IMPOSSIBLE", "msg": "No se puede cumplir la demanda EXACTA con las reglas actuales."}

    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}
