/*
 * argos_listen — l'autre bout du dialecte, en C (PORTFOLIO §1.3).
 *
 * Écoute un port UDP, décode les ARGOS_TARGET, les affiche. C'est tout.
 *
 * Ce qui compte n'est pas ce fichier, c'est ce qu'il N'Y A PAS dedans :
 * aucune connaissance du format. Pas d'offset, pas de taille de champ, pas de
 * CRC. Tout ça vient de <argos/mavlink.h>, produit par mavgen à partir du même
 * argos.xml que le module Python de la console. Deux langages, deux processus,
 * une seule source de vérité — et le jour où un champ change dans le XML, les
 * deux bouts changent ensemble ou aucun ne change.
 *
 * Compilation : make -C .. listen        (depuis mavlink/c_demo/)
 * Usage       : ./argos_listen [port]    (défaut 14650)
 */
#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <argos/mavlink.h>

#define PORT_DEFAUT 14650

static const char *classe(uint8_t c)
{
    switch (c) {
    case ARGOS_CLASS_PERSON:  return "personne";
    case ARGOS_CLASS_VEHICLE: return "vehicule";
    default:                  return "inconnue";
    }
}

static const char *verrou(uint8_t s)
{
    switch (s) {
    case ARGOS_LOCK_TRACK: return "TRACK";
    case ARGOS_LOCK_COAST: return "coast";
    case ARGOS_LOCK_LOST:  return "perdu";
    default:               return "idle ";
    }
}

/* Horloge murale en microsecondes — la même base que le time_usec du message,
 * ce qui permet de calculer l'âge de l'information à la réception. */
static double maintenant_us(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (double)tv.tv_sec * 1e6 + (double)tv.tv_usec;
}

int main(int argc, char **argv)
{
    int port = (argc > 1) ? atoi(argv[1]) : PORT_DEFAUT;

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); return 1; }

    struct sockaddr_in moi;
    memset(&moi, 0, sizeof moi);
    moi.sin_family = AF_INET;
    moi.sin_addr.s_addr = htonl(INADDR_ANY);
    moi.sin_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&moi, sizeof moi) < 0) { perror("bind"); return 1; }

    printf("argos_listen — UDP %d\n", port);
    printf("  dialecte ARGOS compile en dur dans ce binaire :\n");
    printf("    ARGOS_TARGET  id=%d  charge utile=%d octets  CRC_EXTRA=%d\n",
           MAVLINK_MSG_ID_ARGOS_TARGET, MAVLINK_MSG_ID_ARGOS_TARGET_LEN,
           MAVLINK_MSG_ID_ARGOS_TARGET_CRC);
    printf("  (le CRC_EXTRA doit etre identique cote Python, sinon les deux\n"
           "   bouts rejetteraient mutuellement leurs trames)\n\n");

    uint8_t tampon[2048];
    mavlink_message_t msg;
    mavlink_status_t etat;
    unsigned long recus = 0, autres = 0;

    for (;;) {
        ssize_t n = recv(fd, tampon, sizeof tampon, 0);
        if (n <= 0) continue;

        /* MAVLink se parse octet par octet : la machine à états resynchronise
         * toute seule sur le marqueur 0xFD, donc une trame coupée en deux
         * datagrammes ou un octet parasite ne cassent rien. */
        for (ssize_t i = 0; i < n; i++) {
            if (!mavlink_parse_char(MAVLINK_COMM_0, tampon[i], &msg, &etat))
                continue;

            if (msg.msgid != MAVLINK_MSG_ID_ARGOS_TARGET) {
                autres++;
                continue;
            }

            mavlink_argos_target_t t;
            mavlink_msg_argos_target_decode(&msg, &t);
            recus++;

            double age_ms = (maintenant_us() - (double)t.time_usec) / 1000.0;

            printf("[%3lu] de %u:%u  %-8s  u=%+.3f v=%+.3f  taille=%.3f  "
                   "conf=%.2f  %s%s  piste #%u (%u ms)  age=%.1f ms",
                   recus, msg.sysid, msg.compid, classe(t.target_class),
                   (double)t.u, (double)t.v, (double)t.size, (double)t.confidence,
                   verrou(t.lock_state),
                   (t.flags & ARGOS_TARGET_FLAG_ENGAGED) ? " ENGAGE" : "",
                   t.track_id, t.track_age_ms, age_ms);

            /* La bibliothèque C tient elle-même le compte des trous dans la
             * suite des numéros de séquence — exactement la mesure que fait
             * control/link.py côté Python, mais offerte par le parseur. */
            if (etat.packet_rx_drop_count)
                printf("   [perdus: %u]", etat.packet_rx_drop_count);
            if (autres)
                printf("   [+%lu autres messages]", autres);
            printf("\n");
            fflush(stdout);
        }
    }
    return 0;
}
