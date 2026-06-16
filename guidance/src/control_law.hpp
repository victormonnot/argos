#pragma once
#include <algorithm>

namespace argos {

// Loi de guidage en lacet — contrôleur proportionnel (P).
//
// C'EST LE CŒUR D'ARGOS MODE B : transformer un écart de cible en une vitesse
// de rotation. Aujourd'hui l'erreur vient d'un cap simulé ; en S5 elle viendra
// de l'offset horizontal de la détection vidéo (cible à gauche/droite du centre
// image). La loi, elle, ne change pas — c'est pour ça qu'on l'isole et qu'on la teste.
//
//   error        : écart à annuler (cible centrée => 0 ; le signe donne le côté).
//   kp           : gain proportionnel (combien on réagit à l'erreur).
//   max_rate_dps : saturation, deg/s — on ne tourne JAMAIS plus vite (sécurité).
// Retour : la consigne de vitesse de lacet (deg/s).
inline double yaw_rate_command(double error, double kp, double max_rate_dps)
{
    const double rate = kp * error;
    return std::clamp(rate, -max_rate_dps, max_rate_dps);
}

}  // namespace argos
