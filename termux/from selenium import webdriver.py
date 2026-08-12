from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import time

# Configuration du navigateur (ici Chrome)
options = webdriver.ChromeOptions()
# options.add_argument('--headless')  # Décommentez pour exécuter en arrière-plan (plus rapide)
options.add_argument('--disable-gpu')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')

# Initialisation du driver
driver = webdriver.Chrome(options=options)

# URL cible
url = "https://ngl.link/meduso2"

# Fonction pour envoyer un message
def send_message(text):
    try:
        # Attend que la page et le champ texte soient chargés
        wait = WebDriverWait(driver, 5)
        textarea = wait.until(EC.presence_of_element_located((By.XPATH, "/html/body/div[2]/form/div[1]/div[2]/textarea")))
        
        # Efface le contenu et entre le nouveau texte
        textarea.clear()
        textarea.send_keys(text)
        
        # Trouve et clique sur le bouton d'envoi (souvent un bouton de type submit)
        # Adaptez le sélecteur si nécessaire - exemple avec le bouton "Envoyer"
        submit_button = driver.find_element(By.XPATH, "//button[@type='submit']")
        submit_button.click()
        
        # Attente très courte pour laisser le temps à l'envoi de se faire
        time.sleep(0.2)  # Réduisez à 0.1 si le site le permet
        
        # Recharge la page pour être prêt pour le prochain envoi
        driver.get(url)  # Le rechargement est plus fiable qu'attendre un nouvel élément
        
        return True
    except (TimeoutException, NoSuchElementException) as e:
        print(f"Erreur lors de l'envoi du message: {e}")
        # En cas d'erreur, on recharge la page pour tenter de rétablir
        driver.get(url)
        return False

# Message que vous voulez envoyer
mon_message = "ENZO VAUGARNY QUI HABITE AU 23 RUE MAURICE MALLET"  # Changez ce texte

# Nombre de fois que vous voulez envoyer le message
nombre_envies = 10  # Mettez le nombre souhaité

# Chargement initial de la page
driver.get(url)

# Boucle d'envoi
for i in range(nombre_envies):
    print(f"Envoi {i+1}/{nombre_envies}...")
    success = send_message(mon_message)
    if not success:
        print(f"Échec de l'envoi {i+1}, tentative de reprise...")
        time.sleep(1)  # Pause en cas d'échec

print("Terminé !")
# Fermeture du navigateur (commentez si vous voulez garder la fenêtre ouverte)
driver.quit()