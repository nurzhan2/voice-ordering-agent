.PHONY: up down ps logs menu sip wa console chat smoke orders demo demo-clear iiko

up:            ## поднять всё
	docker compose up -d --build

dev:           ## только база и бэкенд, для локальной отладки
	docker compose up -d postgres redis backend

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f agent wa-bot backend

menu:          ## применить изменения menu.yaml
	docker compose restart backend

sip:           ## настроить SIP-транк Новофона
	bash scripts/setup_sip.sh

wa:            ## подписать вебхук WhatsApp
	docker compose exec wa-bot python wa_bot.py setup

console:       ## голосовой разговор с ботом (нужен микрофон)
	cd agent && python agent.py console

chat:          ## текстовый разговор с ботом, дёшево
	cd agent && python chat.py

smoke:         ## автопроверка всей цепочки
	cd agent && python smoke.py

smoke-llm:     ## то же плюс проверка скрипта продаж на живой модели
	cd agent && python smoke.py --llm

demo:          ## демо-заказы для показа
	docker compose exec backend python -m app.seed_demo

demo-clear:    ## убрать демо-заказы
	docker compose exec backend python -m app.seed_demo clear

iiko:          ## выгрузить меню из iiko в menu_iiko.yaml
	docker compose exec backend python -m app.import_iiko > menu_iiko.yaml
	@echo "Готово. Просмотри menu_iiko.yaml глазами перед заменой menu.yaml"

orders:
	@curl -s localhost:8000/admin/orders \
	  -H "Authorization: Bearer $$(grep BACKEND_TOKEN .env | cut -d= -f2)" \
	  | python3 -m json.tool
