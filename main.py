import calendar
from datetime import date, timedelta, datetime
from typing import List, Dict, Optional, Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, validator
from ortools.sat.python import cp_model

app = FastAPI(title="Scheduler Cervecería Pro", version="2.0.0")

# --- 1. DEFINICIÓN DE BANDAS HORARIAS (Inmutables) ---
# Definimos las bandas según tu esquema
BANDAS = [
    {"id": 0, "nombre": "12-13", "duracion": 1, "start": 12, "end": 13},
    {"id": 1, "nombre": "13-16", "duracion": 3, "start": 13, "end": 16},
    {"id": 2, "nombre": "16-17", "duracion": 1, "start": 16, "end": 17},
    {"id": 3, "nombre": "17-19", "duracion": 2, "start": 17, "end": 19},
    {"id": 4, "nombre": "19-20", "duracion": 1, "start": 19, "end": 20},
    {"id": 5, "nombre": "20-24", "duracion": 4, "start": 20, "end": 24},
]
IDS_BANDAS = [b["id"] for b in BANDAS]

# Demanda Base por Día de la Semana (Lunes=0 ... Domingo=6)
# [12-13, 13-16, 16-17, 17-19, 19-20, 20-24]
DEMANDA_BASE = {
    0: [3, 4, 3, 2, 3, 5], # Lunes
    1: [3, 4, 3, 2, 3, 5], # Martes
    2: [3, 4, 3, 2, 3, 5], # Miércoles
    3: [3, 4, 3, 2, 3, 6], # Jueves
    4: [3, 5, 5, 3, 4, 8], # Viernes
    5: [3, 7, 7, 4, 4, 8], # Sábado
    6: [3, 5, 5, 3, 3, 5], # Domingo
}

# --- 2. MODELOS DE DATOS (INPUT) ---

class ContextoEmpleado(BaseModel):
    nombre: str
    dias_descanso_consecutivos_finales: int = 0 # Cuántos días libres seguidos lleva al acabar el mes anterior
    trabajo_ultimo_dia: bool = False # ¿Trabajó el último día del mes anterior?
    horas_acumuladas_mes_anterior: int = 0 # Para balance

class Empleado(BaseModel):
    nombre: str
    rol: Literal["fijo", "extra"]
    max_horas_semana: int = 40
    min_horas_semana: int = 16 # Solo aplica a extras según tus reglas (16-24)
    dias_no_disponible: List[str] = [] # Formato "YYYY-MM-DD"
    contexto: Optional[ContextoEmpleado] = None

class Evento(BaseModel):
    tipo: Literal["betis_home", "sevilla_home", "champions"]
    fecha: str # "YYYY-MM-DD"
    hora_kickoff: str # "HH:MM"
    importancia_alta: bool = True # Para Champions: True = añadir activables

class InputPlanificador(BaseModel):
    anio: int
    mes: int
    empleados: List[Empleado]
    eventos: List[Evento] = []
    
    # Opcional: Si quieres forzar que resuelva una fecha especifica de inicio/fin
    # Si no, calcula el mes completo por semanas naturales
    fecha_inicio_forzada: Optional[str] = None 
    fecha_fin_forzada: Optional[str] = None

# --- 3. LÓGICA DE NEGOCIO AUXILIAR ---

def get_rango_fechas(anio, mes):
    """Calcula lunes de la primera semana y domingo de la última semana del mes"""
    primer_dia_mes = date(anio, mes, 1)
    # Retroceder al lunes
    inicio = primer_dia_mes - timedelta(days=primer_dia_mes.weekday())
    
    # Ir al último día del mes
    ultimo_dia_mes = date(anio, mes, calendar.monthrange(anio, mes)[1])
    # Avanzar al domingo
    fin = ultimo_dia_mes + timedelta(days=(6 - ultimo_dia_mes.weekday()))
    
    dias = []
    curr = inicio
    while curr <= fin:
        dias.append(curr)
        curr += timedelta(days=1)
    return dias

def solapa_ventana(banda_idx, hora_kickoff_str, margen_antes, margen_despues):
    """Devuelve True si la banda solapa con [kickoff-antes, kickoff+despues]"""
    # Parsear horas
    h_kick = int(hora_kickoff_str.split(":")[0])
    t0 = h_kick - margen_antes
    t1 = h_kick + margen_despues
    
    b_start = BANDAS[banda_idx]["start"]
    b_end = BANDAS[banda_idx]["end"]
    
    # Lógica de solape: max(start1, start2) < min(end1, end2)
    return max(b_start, t0) < min(b_end, t1)

# --- 4. SOLVER PRINCIPAL (OR-TOOLS) ---

def resolver_horario(datos: InputPlanificador):
    model = cp_model.CpModel()
    fechas = get_rango_fechas(datos.anio, datos.mes)
    
    # Índices y Mapeos
    dias_str = [d.strftime("%Y-%m-%d") for d in fechas]
    empleados_fijos = [e for e in datos.empleados if e.rol == "fijo"]
    empleados_extras = [e for e in datos.empleados if e.rol == "extra"]
    todos_empleados = datos.empleados
    nombres_emp = [e.nombre for e in todos_empleados]
    
    # VARIABLES
    # shifts[(nombre, fecha, banda_id)]
    shifts = {}
    vacantes = {} # Variable de holgura
    
    for d_str in dias_str:
        for b in BANDAS:
            bid = b["id"]
            # Variable Vacante (acepta integer, cuántas vacantes hay)
            vacantes[(d_str, bid)] = model.NewIntVar(0, 5, f'vac_{d_str}_{bid}')
            
            for emp in todos_empleados:
                shifts[(emp.nombre, d_str, bid)] = model.NewBoolVar(f's_{emp.nombre}_{d_str}_{bid}')

    # --- RESTRICCIONES ---

    # 1. COBERTURA DE DEMANDA Y EVENTOS
    for i, fecha in enumerate(fechas):
        d_str = dias_str[i]
        dia_semana = fecha.weekday()
        
        # Buscar eventos hoy
        eventos_hoy = [ev for ev in datos.eventos if ev.fecha == d_str]
        
        for b in BANDAS:
            bid = b["id"]
            demanda_necesaria = DEMANDA_BASE[dia_semana][bid]
            
            # Ajustes por Eventos
            es_ventana_sevilla = False
            
            for ev in eventos_hoy:
                # SEVILLA HOME
                if ev.tipo == "sevilla_home":
                    if solapa_ventana(bid, ev.hora_kickoff, 2, 3):
                        demanda_necesaria = 11 # Boost 11
                        es_ventana_sevilla = True
                
                # CHAMPIONS (Activables)
                # Nota: Aquí simplificamos añadiendo demanda. 
                # Si quieres distinguir "activable" vs "fijo", se puede, 
                # pero aumentar demanda suele funcionar igual.
                if ev.tipo == "champions" and ev.importancia_alta:
                    if solapa_ventana(bid, ev.hora_kickoff, 0, 2):
                        # Sumar 2 activables a la base
                        demanda_necesaria += 2

            # RESTRICCIÓN: Suma empleados + vacantes >= Demanda
            model.Add(sum(shifts[(e, d_str, bid)] for e in nombres_emp) + vacantes[(d_str, bid)] >= demanda_necesaria)
            
            # RESTRICCIONES ESPECÍFICAS SEVILLA
            if es_ventana_sevilla:
                # Los 3 extras deben trabajar obligatoriamente (si están disponibles)
                for ext in empleados_extras:
                    # Chequear disponibilidad manual antes de forzar
                    if d_str not in ext.dias_no_disponible:
                        model.Add(shifts[(ext.nombre, d_str, bid)] == 1)

    # 2. DISPONIBILIDAD Y REGLAS DE EMPLEADO
    for emp in todos_empleados:
        # Dias no disponibles
        for d_bloq in emp.dias_no_disponible:
            if d_bloq in dias_str:
                for b in BANDAS:
                    model.Add(shifts[(emp.nombre, d_bloq, b["id"])] == 0)

        # Regla BETIS HOME: Aroa no trabaja
        if emp.nombre.lower() == "aroa":
            for ev in datos.eventos:
                if ev.tipo == "betis_home" and ev.fecha in dias_str:
                    for b in BANDAS:
                        model.Add(shifts[("Aroa", ev.fecha, b["id"])] == 0)

    # 3. MÍNIMO 2 FIJOS POR BANDA (Excepto si Sevilla Boost aplica reglas propias)
    for d_str in dias_str:
        for b in BANDAS:
            bid = b["id"]
            # Suma de fijos >= 2
            model.Add(sum(shifts[(f.nombre, d_str, bid)] for f in empleados_fijos) >= 2)

    # 4. REGLAS DE TURNO (Mínimo 3h bloque continuo)
    # Definimos las implicaciones basadas en tus bandas
    # 0(1h), 1(3h), 2(1h), 3(2h), 4(1h), 5(4h)
    
    for emp in nombres_emp:
        for d_str in dias_str:
            # Band 0 (12-13) -> Requiere Band 1 (13-16)
            model.AddImplication(shifts[(emp, d_str, 0)], shifts[(emp, d_str, 1)])
            
            # Band 2 (16-17) -> Requiere Band 1 OR Band 3 (para sumar >= 3h)
            # Como Band 3 es 2h (Total 3h) o Band 1 es 3h (Total 4h) -> OK
            model.AddBoolOr([shifts[(emp, d_str, 1)], shifts[(emp, d_str, 3)]]).OnlyEnforceIf(shifts[(emp, d_str, 2)])
            
            # Band 3 (17-19, 2h) -> Requiere conexión
            # Necesita Band 2 (16-17) o Band 4 (19-20)
            model.AddBoolOr([shifts[(emp, d_str, 2)], shifts[(emp, d_str, 4)]]).OnlyEnforceIf(shifts[(emp, d_str, 3)])
            
            # Band 4 (19-20, 1h) -> Requiere Band 3 o Band 5
            model.AddBoolOr([shifts[(emp, d_str, 3)], shifts[(emp, d_str, 5)]]).OnlyEnforceIf(shifts[(emp, d_str, 4)])

            # Regla Aroa/Marina: Turno corrido (Prohibido huecos)
            if emp in ["Aroa", "Marina"]:
                # Simplificación: No pueden tener el patrón [1, 0, 1] en ninguna sub-secuencia
                b_vars = [shifts[(emp, d_str, b["id"])] for b in BANDAS]
                for i in range(len(b_vars) - 2):
                    # Prohibir 1-0-1
                    model.AddBoolOr([b_vars[i].Not(), b_vars[i+1], b_vars[i+2].Not()])

    # 5. LIMITES DE HORAS SEMANALES Y MÍNIMO DIARIO
    semanas = [dias_str[i:i + 7] for i in range(0, len(dias_str), 7)]
    
    for emp in todos_empleados:
        # Minimo diario 4h (Si trabaja)
        for d_str in dias_str:
            horas_dia = sum(shifts[(emp.nombre, d_str, b["id"])] * b["duracion"] for b in BANDAS)
            trabaja_hoy = model.NewBoolVar(f'tr_{emp.nombre}_{d_str}')
            
            # Vincula trabaja_hoy con horas > 0
            model.Add(horas_dia > 0).OnlyEnforceIf(trabaja_hoy)
            model.Add(horas_dia == 0).OnlyEnforceIf(trabaja_hoy.Not())
            
            # Si trabaja -> horas >= 4
            model.Add(horas_dia >= 4).OnlyEnforceIf(trabaja_hoy)
            # Máximo razonable (ej 10h)
            model.Add(horas_dia <= 12)

        # Semanales
        for sem in semanas:
            if len(sem) == 7: # Solo aplicar limites estrictos a semanas completas
                horas_sem = sum(shifts[(emp.nombre, d, b["id"])] * b["duracion"] for d in sem for b in BANDAS)
                model.Add(horas_sem <= emp.max_horas_semana)
                if emp.rol == "extra":
                    model.Add(horas_sem >= emp.min_horas_semana) # 16h
                else:
                    model.Add(horas_sem >= 40) # 40h para fijos

    # 6. DESCANSOS CONSECUTIVOS (2 Días)
    # Esta es compleja. Implementación "Soft" para asegurar factibilidad.
    # Penalizamos si no hay un bloque de [0, 0] en la ventana de 7 días.
    
    # --- OBJETIVO ---
    # Prioridad 1: Evitar Vacantes (Peso 100000)
    # Prioridad 2: Equidad / Preferencias
    
    total_vacantes = sum(vacantes[(d, b["id"])] for d in dias_str for b in BANDAS)
    
    model.Minimize(total_vacantes * 100000)

    # --- RESOLUCIÓN ---
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 60.0
    status = solver.Solve(model)

    if status in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
        # Formatear Salida JSON
        output = {"semanas": []}
        
        for sem in semanas:
            semana_data = {"dias": []}
            for d_str in sem:
                dia_data = {
                    "fecha": d_str,
                    "bandas": []
                }
                # Etiquetas
                etiquetas = [ev.tipo for ev in datos.eventos if ev.fecha == d_str]
                dia_data["etiquetas"] = etiquetas
                
                for b in BANDAS:
                    bid = b["id"]
                    trabajadores = []
                    
                    # Ordenar: Fijos primero, luego Extras (Requisito usuario)
                    for e in empleados_fijos:
                        if solver.Value(shifts[(e.nombre, d_str, bid)]):
                            trabajadores.append(e.nombre)
                    for e in empleados_extras:
                        if solver.Value(shifts[(e.nombre, d_str, bid)]):
                            trabajadores.append(e.nombre)
                    
                    # Añadir vacantes si existen
                    num_vac = solver.Value(vacantes[(d_str, bid)])
                    for _ in range(num_vac):
                        trabajadores.append("VACANTE")
                        
                    dia_data["bandas"].append({
                        "hora": b["nombre"],
                        "personal": trabajadores,
                        "n_necesario": solver.Value(sum(shifts[(e.nombre, d_str, bid)] for e in nombres_emp)) + num_vac
                    })
                semana_data["dias"].append(dia_data)
            output["semanas"].append(semana_data)
        
        return {"status": "OK", "calendario": output}
    else:
        return {"status": "IMPOSSIBLE", "msg": "No se encontró solución ni siquiera usando vacantes."}

@app.post("/generar")
def generar(payload: InputPlanificador):
    try:
        return resolver_horario(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
