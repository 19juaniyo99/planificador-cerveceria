# Usamos una versión ligera de Python
FROM python:3.9-slim

# Configuraciones de entorno para que Python no genere archivos basura
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Creamos la carpeta de trabajo dentro del contenedor
WORKDIR /app

# Copiamos e instalamos las dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el código fuente (asegúrate de que main.py esté en la misma carpeta)
COPY main.py .

# Exponemos el puerto 8000
EXPOSE 8000

# Comando de arranque
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
