/*
 * argos_listen_cpp — le même consommateur, en C++11 (PORTFOLIO §1.3).
 *
 * Même argos.xml, même message, même CRC_EXTRA — mais l'API générée par
 * `mavgen --lang=C++11` n'a rien à voir avec celle du C :
 *
 *   C          struct plate + fonctions   mavlink_msg_argos_target_decode(&msg, &t)
 *   C++11      classe dans un namespace   mavlink::argos::msg::ARGOS_TARGET
 *              constantes constexpr       ARGOS_TARGET::CRC_EXTRA (connu à la COMPILATION)
 *              introspection générée      t.to_yaml()
 *
 * Les deux couches coexistent : le C++ utilise la machine à états du parseur C
 * (mavlink_parse_char), puis désérialise dans l'objet C++. C'est exactement le
 * découpage de MAVSDK / MAVROS — et c'est pour ça qu'ArduPilot, qui est du C++,
 * embarque les en-têtes C.
 *
 * Compilation : make -C .. listen-cpp
 * Usage       : ./argos_listen_cpp [port]     (défaut 14650)
 */
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>

// UN SEUL en-tête. Ne PAS ajouter <argos/mavlink.h> à côté : l'en-tête C définit
// `MAVLINK_VERSION` comme macro, la version C++ le déclare comme constexpr, et le
// préprocesseur réécrit alors la déclaration en `constexpr auto 2 = 2;`. Les deux
// API ne se mélangent pas — le .hpp embarque déjà tout ce qu'il faut.
#include <argos/argos.hpp>

using Cible = mavlink::argos::msg::ARGOS_TARGET;

// ── LE BRANCHEMENT QUE LE C++ EXIGE ET QUE LE C OFFRE ───────────────────────
// En C, mavlink_get_msg_entry() est fournie clé en main : le parseur sait tout
// seul retrouver, pour un identifiant, la longueur et le CRC_EXTRA attendus.
// En C++11, message.hpp pose `#define MAVLINK_GET_MSG_ENTRY` et laisse
// explicitement ce trou à remplir — la table est fournie (MESSAGE_ENTRIES), le
// câblage est à l'application. C'est un choix de conception : une appli qui
// route plusieurs dialectes doit pouvoir décider elle-même quelle table
// consulter. Ces dix lignes, c'est exactement le « middleware » du poste visé.
namespace mavlink {
const mavlink_msg_entry_t *mavlink_get_msg_entry(uint32_t msgid)
{
    const auto &table = argos::MESSAGE_ENTRIES;      // triée par msgid
    auto it = std::lower_bound(table.begin(), table.end(), msgid,
                               [](const mavlink_msg_entry_t &e, uint32_t id) {
                                   return e.msgid < id;
                               });
    if (it == table.end() || it->msgid != msgid)
        return nullptr;                              // message inconnu -> trame rejetée
    return &(*it);
}
}  // namespace mavlink

/* Note de génération : le générateur C++11 retire le préfixe commun entre le nom
 * de l'enum et celui de ses entrées. `ARGOS_CLASS_PERSON` en C devient donc
 * `ARGOS_TARGET_CLASS::CLASS_PERSON` en C++ — le préfixe est porté par le type,
 * plus par le nom. Piège classique quand on porte du code d'une API à l'autre. */
static const char *classe(uint8_t c)
{
    using mavlink::argos::ARGOS_TARGET_CLASS;
    switch (static_cast<ARGOS_TARGET_CLASS>(c)) {
    case ARGOS_TARGET_CLASS::CLASS_PERSON:  return "personne";
    case ARGOS_TARGET_CLASS::CLASS_VEHICLE: return "vehicule";
    default:                                return "inconnue";
    }
}

static const char *verrou(uint8_t s)
{
    using mavlink::argos::ARGOS_LOCK_STATE;
    switch (static_cast<ARGOS_LOCK_STATE>(s)) {
    case ARGOS_LOCK_STATE::TRACK: return "TRACK";
    case ARGOS_LOCK_STATE::COAST: return "coast";
    case ARGOS_LOCK_STATE::LOST:  return "perdu";
    default:                      return "idle ";
    }
}

static double maintenant_us()
{
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    return static_cast<double>(tv.tv_sec) * 1e6 + static_cast<double>(tv.tv_usec);
}

int main(int argc, char **argv)
{
    const int port = (argc > 1) ? std::atoi(argv[1]) : 14650;
    const bool verbeux = (argc > 2 && std::string(argv[2]) == "-v");

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); return 1; }

    struct sockaddr_in moi {};
    moi.sin_family = AF_INET;
    moi.sin_addr.s_addr = htonl(INADDR_ANY);
    moi.sin_port = htons(static_cast<uint16_t>(port));
    if (bind(fd, reinterpret_cast<struct sockaddr *>(&moi), sizeof moi) < 0) {
        perror("bind"); return 1;
    }

    // Ces trois valeurs sont des constexpr : elles ne sont pas lues quelque part
    // au démarrage, elles sont gravées dans le binaire à la compilation depuis
    // argos.xml. Une incohérence de format ne peut donc pas apparaître à l'exécution.
    std::cout << "argos_listen_cpp — UDP " << port << "\n"
              << "  " << Cible::NAME << "  id=" << Cible::MSG_ID
              << "  charge utile=" << Cible::LENGTH
              << "  CRC_EXTRA=" << +Cible::CRC_EXTRA << "   (constexpr)\n"
              << "  -v pour le YAML complet de chaque message\n\n";

    // En mode C++11, l'API C elle-même est aspirée dans `namespace mavlink` :
    // le type mavlink_message_t existe toujours, mais il s'appelle désormais
    // mavlink::mavlink_message_t. Le C n'a pas de namespace, le C++ en met un.
    uint8_t tampon[2048];
    mavlink::mavlink_message_t msg;
    mavlink::mavlink_status_t etat;
    unsigned long recus = 0, autres = 0;

    for (;;) {
        ssize_t n = recv(fd, tampon, sizeof tampon, 0);
        if (n <= 0) continue;

        for (ssize_t i = 0; i < n; i++) {
            if (!mavlink::mavlink_parse_char(mavlink::MAVLINK_COMM_0, tampon[i],
                                             &msg, &etat))
                continue;

            if (msg.msgid != Cible::MSG_ID) { autres++; continue; }

            // Désérialisation C++ : MsgMap lit la charge utile du message C et
            // remplit les champs typés de l'objet. Aucun offset écrit à la main.
            mavlink::MsgMap map(&msg);
            Cible t;
            t.deserialize(map);
            recus++;

            const double age_ms = (maintenant_us() - static_cast<double>(t.time_usec)) / 1000.0;

            std::printf("[%3lu] de %u:%u  %-8s  u=%+.3f v=%+.3f  taille=%.3f  "
                        "conf=%.2f  %s%s  piste #%u (%u ms)  age=%.1f ms",
                        recus, msg.sysid, msg.compid, classe(t.target_class),
                        static_cast<double>(t.u), static_cast<double>(t.v),
                        static_cast<double>(t.size), static_cast<double>(t.confidence),
                        verrou(t.lock_state),
                        (t.flags & uint8_t(mavlink::argos::ARGOS_TARGET_FLAGS::FLAG_ENGAGED))
                            ? " ENGAGE" : "",
                        t.track_id, t.track_age_ms, age_ms);
            if (etat.packet_rx_drop_count)
                std::printf("   [perdus: %u]", etat.packet_rx_drop_count);
            if (autres)
                std::printf("   [+%lu autres messages]", autres);
            std::printf("\n");

            // to_yaml() est généré depuis le XML : le programme sait décrire un
            // message qu'aucun humain ne lui a décrit. C'est ce que le C n'a pas.
            if (verbeux)
                std::cout << t.to_yaml() << std::endl;
            std::fflush(stdout);
        }
    }
}
