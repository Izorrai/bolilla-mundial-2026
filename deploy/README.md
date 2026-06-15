# Despliegue en servidor propio (Docker + Cloudflare Tunnel)

Esta guia despliega `bolilla.agaleus.com` en el servidor `servidordocker` (Linux + Docker + tunel `recogidas`).

## Lo que monta

```
bolilla.agaleus.com ──[Cloudflare Tunnel "recogidas"]──> 192.168.254.112:5050 ──[nginx]──> /home/agaleus/bolilla
                                                                                                 ^
                                                                                                 |
                                                                                 cron */5min en host (git pull + scripts)
```

## Instalacion (una sola vez)

Conectate al servidor por SSH (Bitvise) como `agaleus` y pega esto:

```bash
# 1. Clonar el repo en home
cd ~
git clone https://github.com/Izorrai/bolilla-mundial-2026.git bolilla
cd bolilla

# 2. Crear .env con la API key de football-data.org
cp deploy/.env.example .env
nano .env   # edita: FOOTBALL_DATA_API_KEY=tu_key_aqui
chmod 600 .env

# 3. Levantar nginx en port 5050
cd deploy
docker compose up -d
docker ps | grep bolilla

# 4. Probar localmente
curl -I http://localhost:5050/
curl http://localhost:5050/data/teams.json | head -20

# 5. Primera ejecucion manual del cron (para generar los JSON)
cd ..
bash deploy/cron-update.sh

# 6. Registrar el cron en el usuario agaleus (cada 5 min)
( crontab -l 2>/dev/null; echo "*/5 * * * * cd /home/agaleus/bolilla && bash deploy/cron-update.sh >> /home/agaleus/bolilla/deploy/cron.log 2>&1" ) | sort -u | crontab -
crontab -l
```

## Configurar Cloudflare Tunnel

El tunel `recogidas` ya esta corriendo. Solo hay que anadir el host y el DNS.

```bash
# 7. Crear el CNAME en Cloudflare (usa el cert.pem que ya tienes en home)
cloudflared tunnel route dns recogidas bolilla.agaleus.com

# 8. Anadir la ruta al config.yml del tunel (root)
sudo nano /etc/cloudflared/config.yml
```

Anade el bloque marcado al final de `ingress:`, **antes** del `service: http_status:404`:

```yaml
ingress:
  - hostname: recogidas.agaleus.com
    service: http://192.168.254.112:5002

  - hostname: epis.agaleus.com
    service: http://192.168.254.112:6080

  - hostname: hub.agaleus.com
    service: http://192.168.254.112:9090

  - hostname: firmas.agaleus.com
    service: http://192.168.254.112:5000

  # >>> NUEVO <<<
  - hostname: bolilla.agaleus.com
    service: http://192.168.254.112:5050

  - service: http_status:404
```

```bash
# 9. Reiniciar cloudflared
sudo systemctl restart cloudflared
sudo systemctl status cloudflared | head -10

# 10. Verificar desde fuera (en ~30s tras el restart)
curl -I https://bolilla.agaleus.com
```

## Operativa diaria

- **Actualizar HTML/JS/scripts**: lo haces como hasta ahora — editas localmente, `git commit && git push`. El cron del servidor hace `git pull` cada 5 min.
- **Importar inscripciones nuevas**: tu (organizador) usas `admin.html` o `porra-admin.html` desde el navegador, exportas el JSON, lo guardas en `data/rooms/<sala>/participants.json` (o `data/porra/predictions.json`), `git commit && git push`. En 5 min el servidor lo recoge y refresca el ranking.
- **Resultados oficiales** (pichichi, MVP, etc.): mismo flujo desde el admin.

## Ver logs y diagnostico

```bash
# Cron
tail -f /home/agaleus/bolilla/deploy/cron.log

# Nginx (en el contenedor)
docker logs -f bolilla-web

# Cloudflared
sudo journalctl -u cloudflared -f
```

## Parar / reiniciar

```bash
cd /home/agaleus/bolilla/deploy
docker compose down       # parar
docker compose up -d      # arrancar
docker compose restart    # reiniciar
```

## Quitar todo

```bash
# Sacar el cron
crontab -l | grep -v "/home/agaleus/bolilla" | crontab -

# Parar y borrar el container
cd /home/agaleus/bolilla/deploy
docker compose down

# Quitar la ruta del tunel: edita /etc/cloudflared/config.yml y borra el bloque bolilla, despues
sudo systemctl restart cloudflared

# Borrar la carpeta
rm -rf /home/agaleus/bolilla
```
