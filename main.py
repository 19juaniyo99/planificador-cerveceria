import calendar
from datetime import date, timedelta
from typing import List, Optional, Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from ortools.sat.python import cp_model

app = FastAPI()

# --- 1. DEFINICIÓN DE BANDAS ---
BANDAS = [
    {"id": 0, "nombre": "12-13", "duracion": 1, "start": 12, "end": 13},
    {"id": 1, "nombre": "13-16", "duracion": 3, "start": 13, "end": 16},
    {"id": 2, "nombre": "16-17", "duracion": 1, "start": 16, "end": 17},
    {"id": 3, "nombre": "17-19", "duracion": 2, "start": 17, "end": 19},
    {"id": 4, "nombre": "19-20", "duracion": 1, "start": 19, "end": 20},
    {"id": 5, "nombre": "20-24", "duracion": 4, "start": 20, "end": 24},
]
# Demanda por defecto (Lunes=0 ... Domingo=6)
DEMANDA_BASE = {
    0: [3, 4, 3, 2, 3, 5], 1: [3, 4, 3, 2, 3, 5], 2: [3, 4, 3, 2, 3, 5],
    3: [3, 4, 3, 2, 3, 6], 4: [3, 5, 5, 3, 4, 8], 5: [3, 7, 7, 4, 4, 8],
    6: [3, 5, 5, 3, 3, 5]
}

# --- 2. MODELOS DE DATOS ---
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

# --- 3. LÓGICA AUXILIAR ---
def get_rango_fechas(anio, mes):
    primer_dia = date(anio, mes, 1)
    inicio = primer_dia - timedelta(days=primer_dia.weekday())
    ultimo_dia = date(anio, mes, calendar.monthrange(anio, mes)[1])
    fin = ultimo_dia + timedelta(days=(6 - ultimo_dia.weekday()))
    
    dias = []
    curr = inicio
    while curr <= fin:
        dias.append(curr)
        curr += timedelta(days=1)
    return dias

def solapa_ventana(banda_idx, hora_kickoff_str, margen_antes, margen_despues):
    h_kick = int(hora_kickoff_str.split(":")[0])
    t0 = h_kick - margen_antes
    t1 = h_kick + margen_despues
    b_start = BANDAS[banda_idx]["start"]
    b_end = BANDAS[banda_idx]["end"]
    return max(b_start, t0) < min(b_end, t1)

# --- 4. SOLVER ---
@app.post("/generar")
def resolver_horario(datos: InputPlanificador):
    try:
        model = cp_model.CpModel()
        fechas = get_rango_fechas(datos.anio, datos.mes)
        dias_str = [d.strftime("%Y-%m-%d") for d in fechas]
        
        # Mapeos vitales (Aquí estaba el error antes)
        nombres_emp = [e.nombre for e in datos.empleados]
        empleados_map = {e.nombre: e for e in datos.empleados}
        
        shifts = {}
        vacantes = {}

        # Crear variables
        for d_str in dias_str:
            for b in BANDAS:
                bid = b["id"]
                vacantes[(d_str, bid)] = model.NewIntVar(0, 5, f'vac_{d_str}_{bid}')
                for nombre in nombres_emp:
                    shifts[(nombre, d_str, bid)] = model.NewBoolVar(f's_{nombre}_{d_str}_{bid}')

        # RESTRICCIONES
        for i, fecha in enumerate(fechas):
            d_str = dias_str[i]
            dia_semana = fecha.weekday()
            eventos_hoy = [ev for ev in datos.eventos if ev.fecha == d_str]
            
            for b in BANDAS:
                bid = b["id"]
                demanda = DEMANDA_BASE[dia_semana][bid]
                es_sevilla = False

                for ev in eventos_hoy:
                    if ev.tipo == "sevilla_home" and solapa_ventana(bid, ev.hora_kickoff, 2, 3):
                        demanda = 11
                        es_sevilla = True
                    if ev.tipo == "champions" and ev.importancia_alta and solapa_ventana(bid, ev.hora_kickoff, 0, 2):
                        demanda += 2

                # Cobertura
                model.Add(sum(shifts[(n, d_str, bid)] for n in nombres_emp) + vacantes[(d_str, bid)] >= demanda)
                
                # Regla Sevilla: Extras obligatorios
                if es_sevilla:
                    for nombre in nombres_emp:
                        emp_obj = empleados_map[nombre]
                        if emp_obj.rol == "extra" and d_str not in emp_obj.dias_no_disponible:
                            model.Add(shifts[(nombre, d_str, bid)] == 1)

        # Reglas por empleado
        for nombre in nombres_emp:
            emp_obj = empleados_map[nombre]
            
            # Disponibilidad
            for d_bloq in emp_obj.dias_no_disponible:
                if d_bloq in dias_str:
                    for b in BANDAS:
                        model.Add(shifts[(nombre, d_bloq, b["id"])] == 0)
            
            # Betis Home (Aroa)
            if nombre.lower() == "aroa":
                for ev in datos.eventos:
                    if ev.tipo == "betis_home" and ev.fecha in dias_str:
                        for b in BANDAS:
                            model.Add(shifts[(nombre, ev.fecha, b["id"])] == 0)

            # Turnos continuos y coherencia de bandas
            for d_str in dias_str:
                # 12-13 -> 13-16
                model.AddImplication(shifts[(nombre, d_str, 0)], shifts[(nombre, d_str, 1)])
                # 16-17 -> (13-16 OR 17-19)
                model.AddBoolOr([shifts[(nombre, d_str, 1)], shifts[(nombre, d_str, 3)]]).OnlyEnforceIf(shifts[(nombre, d_str, 2)])
                # 17-19 -> (16-17 OR 19-20)
                model.AddBoolOr([shifts[(nombre, d_str, 2)], shifts[(nombre, d_str, 4)]]).OnlyEnforceIf(shifts[(nombre, d_str, 3)])
                # 19-20 -> (17-19 OR 20-24)
                model.AddBoolOr([shifts[(nombre, d_str, 3)], shifts[(nombre, d_str, 5)]]).OnlyEnforceIf(shifts[(nombre, d_str, 4)])

        # Mínimo 2 fijos
        fijos_names = [e.nombre for e in datos.empleados if e.rol == "fijo"]
        for d_str in dias_str:
            for b in BANDAS:
                model.Add(sum(shifts[(n, d_str, b["id"])] for n in fijos_names) >= 2)

        # Objetivo
        total_vacantes = sum(vacantes[(d, b["id"])] for d in dias_str for b in BANDAS)
        model.Minimize(total_vacantes)

        # Resolver
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 30.0
        status = solver.Solve(model)

        if status in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
            res = {"semanas": []}
            # (Aquí iría el formateo complejo, simplificado para JSON)
            semanas_chunks = [dias_str[i:i + 7] for i in range(0, len(dias_str), 7)]
            
            for sem in semanas_chunks:
                semana_data = []
                for d_str in sem:
                    dia_info = {"fecha": d_str, "turnos": []}
                    for b in BANDAS:
                        bid = b["id"]
                        workers = []
                        for n in fijos_names:
                            if solver.Value(shifts[(n, d_str, bid)]): workers.append(n)
                        extras_names = [e.nombre for e in datos.empleados if e.rol == "extra"]
                        for n in extras_names:
                            if solver.Value(shifts[(n, d_str, bid)]): workers.append(n)
                        
                        # Vacantes
                        nv = solver.Value(vacantes[(d_str, bid)])
                        for _ in range(nv): workers.append("VACANTE")
                        
                        dia_info["turnos"].append({"hora": b["nombre"], "personal": workers})
                    semana_data.append(dia_info)
                res["semanas"].append(semana_data)
            return res
        else:
            return {"status": "Imposible", "msg": "No se encontró solución"}

    except Exception as e:
        import traceback
        return {"error": str(e), "detalle": traceback.format_exc()}
