# CMakeLists.txt — la recette de build du projet guidance (C++/MAVSDK).
# CMake lit ce fichier, génère un Makefile, et compile.
cmake_minimum_required(VERSION 3.15)
project(argos_guidance CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Trouve MAVSDK installé sur le système (le .deb a posé un MAVSDKConfig.cmake).
find_package(MAVSDK REQUIRED)

# Notre exécutable, construit à partir de src/main.cpp.
add_executable(guidance src/main.cpp)

# On le lie à la lib MAVSDK (cible importée fournie par find_package).
target_link_libraries(guidance MAVSDK::mavsdk)
