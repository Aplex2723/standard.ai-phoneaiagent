# Utilizar una imagen base de Python
FROM python:3.11

# Establecer el directorio de trabajo
WORKDIR /app

# Copiar los archivos de requerimientos y instalar dependencias
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

# Copiar todo el contenido de la aplicación
COPY . .

# Exponer el puerto en el que la aplicación escuchará
EXPOSE 8000

# Comando para ejecutar la aplicación
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]

