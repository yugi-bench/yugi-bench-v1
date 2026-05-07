--[[message
Sample puzzle bundled with YuGiOh-Bench as the prompt example.

Your Starting LP: 1500
Opponent's Starting LP: 3100
Complexity: 2/10

Objective: Win this Turn
]]
Debug.ReloadFieldBegin(DUEL_ATTACK_FIRST_TURN+DUEL_SIMPLE_AI,2)
Debug.SetPlayerInfo(0,1500,0,0)
Debug.SetPlayerInfo(1,3100,0,0)
Debug.SetAIName("Sample Opponent")

-- Hand (yours)
Debug.AddCard(83764718,0,0,LOCATION_HAND,0,POS_FACEDOWN) -- Monster Reborn
Debug.AddCard(97590747,0,0,LOCATION_HAND,0,POS_FACEDOWN) -- La Jinn the Mystical Genie of the Lamp

-- Monster Zones (yours)
Debug.AddCard(15025844,0,0,LOCATION_MZONE,0,POS_FACEUP_DEFENSE) -- Mystical Elf (DEF)

-- Graveyard (yours)
Debug.AddCard(89631142,0,0,LOCATION_GRAVE,0,POS_FACEUP) -- Blue-Eyes White Dragon

-- Monster Zones (opponent's)
Debug.AddCard(5053103,1,1,LOCATION_MZONE,1,POS_FACEUP_ATTACK,true) -- Battle Ox
Debug.AddCard(15025844,1,1,LOCATION_MZONE,3,POS_FACEUP_ATTACK,true) -- Mystical Elf (ATK)

Debug.ReloadFieldEnd()
Debug.ShowHint("Win in this turn!")
aux.BeginPuzzle()
